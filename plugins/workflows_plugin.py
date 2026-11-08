"""Plugin entry point for YouTube notes, video editing, and workflow status."""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from pathlib import Path

from plugins.workflows.framework import run_sync
from plugins.workflows.video import FFmpegUnavailable, VideoEditor
from plugins.workflows.youtube import YouTubeNotesWorkflow

PLUGIN = {
    'name': 'workflows',
    'description': 'Run reusable YouTube notes and FFmpeg video editing workflows with progress and verification.',
    'parameters': {
        'type': 'OBJECT',
        'properties': {
            'workflow': {'type': 'STRING', 'description': 'youtube_notes or video_edit'},
            'url': {'type': 'STRING'},
            'operation': {'type': 'STRING', 'description': 'trim, merge, remove_silence, subtitles, music, speed, resize, extract_audio, compress, intro_outro'},
            'source': {'type': 'STRING'},
            'output': {'type': 'STRING'},
            'options': {'type': 'OBJECT'},
            'notes_folder': {'type': 'STRING'},
            'export': {'type': 'STRING', 'description': 'Optional docx or pdf export for YouTube notes'},
        },
        'required': ['workflow'],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one Phase 3 workflow and return verified results."""
    params = dict(parameters or {})
    workflow = str(params.get('workflow') or '').casefold()
    notify = player.write_log if player and hasattr(player, 'write_log') else print
    if workflow == 'youtube_notes':
        url = str(params.get('url') or '').strip()
        if not url:
            return 'A YouTube URL is required.'
        target = run_sync(YouTubeNotesWorkflow(params.get('notes_folder'), notify).run(url, params.get('export')))
        return f'YouTube notes saved and verified: {target}'
    if workflow == 'video_edit':
        source = params.get('source')
        operation = str(params.get('operation') or '').casefold()
        if not source or not operation:
            return 'Video source and operation are required.'
        try:
            if operation == 'music':
                operation = 'background_music'
            plan = {
                'workflow': 'video_edit', 'operation': operation,
                'source': str(source), 'output': str(params.get('output') or ''),
                'options': dict(params.get('options') or {}),
            }
            if player and hasattr(player, 'show_content'):
                player.show_content('VIDEO EDIT PLAN', json.dumps(plan, indent=2))
            result = VideoEditor().edit(operation, source, params.get('output'), notify=notify, **dict(params.get('options') or {}))
        except FFmpegUnavailable as exc:
            return str(exc)
        _open_output_folder(Path(result['output']).parent)
        return f"Video edit complete and verified: {result['output']}"
    return 'Unsupported workflow. Use youtube_notes or video_edit.'


def _open_output_folder(folder: Path) -> None:
    """Open a verified output directory without invoking a shell."""
    try:
        if platform.system() == 'Windows':
            os.startfile(str(folder))
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', str(folder)])
        else:
            subprocess.Popen(['xdg-open', str(folder)])
    except (OSError, AttributeError):
        logging.getLogger('aura.workflows').warning('Could not open output folder: %s', folder)