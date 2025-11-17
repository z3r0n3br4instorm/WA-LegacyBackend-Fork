from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional


FFMPEG_COMMAND = "ffmpeg"


class MediaProcessingError(RuntimeError):
    ...


def _run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        [FFMPEG_COMMAND, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise MediaProcessingError(completed.stderr.decode("utf-8", errors="ignore"))


def convert_audio_to_mp3(audio_bytes: bytes, source_suffix: str = ".ogg") -> Path:
    temp_input = Path(tempfile.mkstemp(suffix=source_suffix)[1])
    temp_output = Path(tempfile.mkstemp(suffix=".mp3")[1])

    temp_input.write_bytes(audio_bytes)
    try:
        _run_ffmpeg(["-y", "-i", str(temp_input), "-acodec", "libmp3lame", str(temp_output)])
        return temp_output
    finally:
        temp_input.unlink(missing_ok=True)


def convert_voice_note_to_ogg(audio_bytes: bytes, source_suffix: str = ".caf") -> Path:
    temp_input = Path(tempfile.mkstemp(suffix=source_suffix)[1])
    temp_output = Path(tempfile.mkstemp(suffix=".ogg")[1])
    temp_input.write_bytes(audio_bytes)
    try:
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(temp_input),
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-ac",
                "1",
                str(temp_output),
            ]
        )
        return temp_output
    finally:
        temp_input.unlink(missing_ok=True)


def convert_video_to_quicktime(video_bytes: bytes) -> Path:
    input_path = Path(tempfile.mkstemp(suffix=".mp4")[1])
    output_path = Path(tempfile.mkstemp(suffix=".mov")[1])
    input_path.write_bytes(video_bytes)
    try:
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(input_path),
                "-vf",
                "scale='min(640,iw)':'min(480,ih)':force_original_aspect_ratio=decrease,fps=30,yadif",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        return output_path
    finally:
        input_path.unlink(missing_ok=True)


def generate_thumbnail_from_video(video_bytes: bytes) -> Path:
    input_path = Path(tempfile.mkstemp(suffix=".mp4")[1])
    output_path = Path(tempfile.mkstemp(suffix=".png")[1])
    input_path.write_bytes(video_bytes)
    try:
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(input_path),
                "-vf",
                "thumbnail,scale=320:240",
                "-frames:v",
                "1",
                str(output_path),
            ]
        )
        return output_path
    finally:
        input_path.unlink(missing_ok=True)
