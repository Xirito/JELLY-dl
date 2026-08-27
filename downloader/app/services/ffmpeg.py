"""Shared ffmpeg post-processing — reusable by every downloader plugin.

yt-dlp invokes ffmpeg internally for its own stream muxing; this class exists
so future plugins (ani-cli, ...) get merge/remux without duplicating logic.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


class FfmpegPostProcessor:
    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg_bin = ffmpeg_bin

    def _run(self, args: list[str]) -> None:
        proc = subprocess.run(
            [self.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise FfmpegError(proc.stderr.strip()[-2000:])

    def merge(self, video: Path, audio: Path, out: Path) -> Path:
        """Mux separate video+audio streams into one container, no re-encode."""
        self._run(["-i", str(video), "-i", str(audio), "-c", "copy",
                   "-map", "0:v:0", "-map", "1:a:0", str(out)])
        return out

    def remux(self, src: Path, out: Path) -> Path:
        """Change container without re-encoding."""
        self._run(["-i", str(src), "-c", "copy", str(out)])
        return out
