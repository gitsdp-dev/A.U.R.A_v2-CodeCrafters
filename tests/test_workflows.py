from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from plugins.workflows.framework import (
    WorkflowContext,
    WorkflowError,
    WorkflowRunner,
    WorkflowStep,
)
from plugins.workflows.video import FFmpegUnavailable, VideoEditor
from plugins.workflows.youtube import (
    TranscriptSegment,
    chunk_transcript,
    parse_youtube_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://youtu.be/abcdefghijk?t=42",
        "https://www.youtube.com/shorts/abcdefghijk?list=PL123",
        "https://www.youtube.com/embed/abcdefghijk",
    ],
)
def test_youtube_url_parser_supports_documented_forms(url):
    reference = parse_youtube_url(url)
    assert reference.video_id == "abcdefghijk"
    assert reference.canonical_url.startswith(
        "https://www.youtube.com/watch?v=abcdefghijk"
    )


def test_youtube_url_parser_rejects_non_youtube_urls():
    with pytest.raises(ValueError):
        parse_youtube_url("https://example.com/watch?v=abcdefghijk")


def test_transcript_chunking_preserves_overlap():
    segments = [
        TranscriptSegment(index * 10, 1, f"segment-{index}-" + "x" * 20)
        for index in range(8)
    ]
    chunks = chunk_transcript(segments, max_chars=70, overlap_chars=20)
    assert len(chunks) > 1
    assert any(
        left[-1].text == right[0].text
        for left, right in itertools.pairwise(chunks)
    )


def test_declarative_workflow_reports_progress_and_verifies():
    events = []

    async def run(_context):
        return "done"

    context = asyncio.run(
        WorkflowRunner(events.append).run(
            [
                WorkflowStep("one", run, lambda value, _: value == "done"),
            ],
            WorkflowContext(),
        )
    )
    assert context.values == {"one": "done"}
    assert events == ["WORKFLOW 1/1: one", "WORKFLOW 1/1: one complete"]


def test_workflow_verification_failure_is_reported():
    with pytest.raises(WorkflowError):
        asyncio.run(
            WorkflowRunner().run(
                [
                    WorkflowStep(
                        "broken", lambda _context: "", lambda value, _: bool(value)
                    ),
                ]
            )
        )


def test_ffmpeg_command_builder_is_non_destructive_and_covers_operations(
    tmp_path: Path,
):
    source = tmp_path / "source.mp4"
    output = tmp_path / "edited.mp4"
    editor = VideoEditor(ffmpeg="ffmpeg", ffprobe="ffprobe")
    for operation, options in (
        ("trim", {"start": 1, "end": 4}),
        ("extract_audio", {}),
        ("remove_silence", {}),
        ("compress", {"crf": 30}),
        ("resize", {"width": 720, "height": 1280}),
        ("crop", {"width": 720, "height": 720, "x": 0, "y": 0}),
        ("speed", {"factor": 3}),
        ("text_overlay", {"text": "AURA", "position": "10:20"}),
        ("merge", {"clips": ["a.mp4", "b.mp4"]}),
    ):
        command = editor.build_command(operation, source, output, **options)
        assert command[0] == "ffmpeg"
        assert str(source.resolve()) in command or operation == "merge"
        assert str(output.resolve()) in command


def test_ffmpeg_missing_is_clear(tmp_path: Path):
    editor = VideoEditor(ffmpeg=None, ffprobe=None)
    with pytest.raises(FFmpegUnavailable, match="FFmpeg"):
        editor.require_tools()


def test_workflow_plugin_is_discoverable():
    from core.plugin_loader import discover_plugins

    registry = discover_plugins(
        Path(__file__).resolve().parents[1] / "plugins", set(), logger=lambda _: None
    )
    assert registry.has("workflows")
