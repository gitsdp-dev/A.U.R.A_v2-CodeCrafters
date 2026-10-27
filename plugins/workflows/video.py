"""Safe FFmpeg command construction, execution, progress, and verification."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Any


class FFmpegUnavailable(RuntimeError):
    """Raised when ffmpeg or ffprobe is not available on PATH."""


class VideoEditor:
    """Build and execute non-destructive FFmpeg video edits."""

    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = ffmpeg or shutil.which('ffmpeg')
        self.ffprobe = ffprobe or shutil.which('ffprobe')

    def require_tools(self) -> None:
        """Ensure both required binaries are available."""
        if not self.ffmpeg or not self.ffprobe:
            raise FFmpegUnavailable('FFmpeg and ffprobe are required. Install FFmpeg and add it to PATH.')

    @staticmethod
    def output_path(source: str | Path, output: str | Path | None = None, suffix: str = 'edited') -> Path:
        """Return a new output path and never reuse the source path."""
        source = Path(source).expanduser().resolve()
        target = Path(output).expanduser().resolve() if output else source.with_name(f'{source.stem}_{suffix}{source.suffix}')
        if target == source:
            raise ValueError('output must be different from source')
        return target

    def build_command(self, operation: str, source: str | Path, output: str | Path,
                      **options: Any) -> list[str]:
        """Build a validated FFmpeg command for one supported operation."""
        source = str(Path(source).resolve())
        output = str(Path(output).resolve())
        operation = operation.casefold()
        base = [self.ffmpeg or 'ffmpeg', '-hide_banner', '-y']
        if operation == 'trim':
            command = base + ['-ss', str(options.get('start', 0)), '-i', source]
            if options.get('end') is not None:
                command += ['-to', str(options['end'])]
            return command + ['-c', 'copy', output]
        if operation == 'extract_audio':
            return base + ['-i', source, '-map', '0:a:0', '-vn', '-c:a', 'copy', output]
        if operation == 'compress':
            crf = max(0, min(51, int(options.get('crf', 28))))
            return base + ['-i', source, '-c:v', 'libx264', '-crf', str(crf), '-c:a', 'aac', output]
        if operation == 'resize':
            width, height = int(options['width']), int(options['height'])
            if width <= 0 or height <= 0:
                raise ValueError('resize dimensions must be positive')
            return base + ['-i', source, '-vf', f'scale={width}:{height}', output]
        if operation == 'crop':
            width, height = int(options['width']), int(options['height'])
            x, y = int(options.get('x', 0)), int(options.get('y', 0))
            if min(width, height) <= 0 or min(x, y) < 0:
                raise ValueError('crop dimensions and offsets must be non-negative')
            return base + ['-i', source, '-vf', f'crop={width}:{height}:{x}:{y}', output]
        if operation == 'speed':
            speed = float(options['factor'])
            if speed <= 0:
                raise ValueError('speed factor must be positive')
            video_filters = f'setpts={1 / speed}*PTS'
            audio_filters = _atempo_chain(speed)
            return base + ['-i', source, '-filter_complex', f'[0:v]{video_filters}[v];[0:a]{audio_filters}[a]', '-map', '[v]', '-map', '[a]', output]
        if operation == 'subtitles':
            subtitle = str(Path(options['subtitle']).resolve())
            return base + ['-i', source, '-vf', f'subtitles={subtitle}', output]
        if operation == 'text_overlay':
            text = str(options['text']).replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\\\'")
            position = str(options.get('position', '10:10'))
            return base + ['-i', source, '-vf', f"drawtext=text='{text}':x={position.split(':')[0]}:y={position.split(':')[-1]}", output]
        if operation == 'background_music':
            music = str(Path(options['music']).resolve())
            return base + ['-i', source, '-i', music, '-filter_complex', '[0:a][1:a]amix=inputs=2:duration=first[a]', '-map', '0:v', '-map', '[a]', output]
        if operation == 'merge':
            clips = [str(Path(item).resolve()) for item in options.get('clips', [])]
            if not clips:
                raise ValueError('merge requires clips')
            inputs = sum((['-i', clip] for clip in clips), [])
            video_inputs = ''.join(f'[{i}:v:0][{i}:a:0]' for i in range(len(clips)))
            return base + inputs + ['-filter_complex', f'{video_inputs}concat=n={len(clips)}:v=1:a=1[v][a]', '-map', '[v]', '-map', '[a]', output]
        if operation == 'remove_silence':
            return base + ['-i', source, '-af', 'silenceremove=start_periods=1:start_duration=0.2:start_threshold=-40dB:stop_periods=1:stop_duration=0.2:stop_threshold=-40dB', output]
        if operation == 'intro_outro':
            intro, outro = str(Path(options['intro']).resolve()), str(Path(options['outro']).resolve())
            return base + ['-i', intro, '-i', source, '-i', outro, '-filter_complex', '[0:v:0][0:a:0][1:v:0][1:a:0][2:v:0][2:a:0]concat=n=3:v=1:a=1[v][a]', '-map', '[v]', '-map', '[a]', output]
        raise ValueError(f'unsupported video operation: {operation}')

    def run(self, command: list[str], timeout: int = 1800,
            notify: Callable[[str], None] | None = None) -> str:
        """Execute FFmpeg without a shell and return captured diagnostics."""
        self.require_tools()
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout)
        output = (result.stdout + ('\n' + result.stderr if result.stderr else '')).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f'FFmpeg exited with {result.returncode}')
        if notify:
            notify('FFmpeg completed')
        return output

    def verify(self, output: str | Path) -> dict[str, Any]:
        """Verify output exists and ffprobe reports streams and duration."""
        self.require_tools()
        target = Path(output)
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError('FFmpeg output is missing or empty')
        result = subprocess.run([self.ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(target)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or 'ffprobe failed')
        data = json.loads(result.stdout)
        if not data.get('streams') or not data.get('format', {}).get('duration'):
            raise RuntimeError('FFmpeg output verification found no usable streams or duration')
        return data

    def edit(self, operation: str, source: str | Path, output: str | Path | None = None,
             notify: Callable[[str], None] | None = None, **options: Any) -> dict[str, Any]:
        """Build, execute, and verify one non-destructive edit."""
        self.require_tools()
        target = self.output_path(source, output, operation)
        target.parent.mkdir(parents=True, exist_ok=True)
        command = self.build_command(operation, source, target, **options)
        if notify:
            notify(f'FFmpeg: {operation}')
        self.run(command, timeout=int(options.get('timeout', 1800)), notify=notify)
        info = self.verify(target)
        return {'output': str(target), 'command': command, 'probe': info}


def _atempo_chain(speed: float) -> str:
    """Build chained atempo filters for factors outside FFmpeg's single-filter range."""
    factors = []
    remaining = speed
    while remaining > 2.0:
        factors.append('atempo=2.0')
        remaining /= 2.0
    while remaining < 0.5:
        factors.append('atempo=0.5')
        remaining /= 0.5
    factors.append(f'atempo={remaining:g}')
    return ','.join(factors)