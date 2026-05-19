"""Music playback via yt-dlp + ffmpeg.

`search_audio_url(query)` resolves a free-form query (song name or URL) to a
streamable media URL. `stream_pcm(url)` spawns an ffmpeg subprocess that
transcodes the source into raw 16 kHz mono int16 PCM and yields it in small
async chunks, suitable for feeding into `pipeline.audio.opus_encode` and a
persistent TCP audio stream to the device.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import yt_dlp

log = logging.getLogger(__name__)

# 16 kHz mono int16 = 32 KB/s. 640 bytes = 20 ms = 1 Opus frame. We read in
# 80 ms chunks so we can re-check cancellation between encode calls without
# starving the device.
CHUNK_BYTES = 640 * 4

_YDL_OPTS: dict[str, Any] = {
    "format": "bestaudio[abr<=160]/bestaudio",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    # If the query isn't a URL, search YouTube and take the top result.
    "default_search": "ytsearch1",
    "extract_flat": False,
}


async def search_audio_url(query: str) -> dict[str, Any]:
    """Resolve a query to a streamable audio URL via yt-dlp."""
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if info.get("entries"):
                info = info["entries"][0]
            return {
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url"),
                "url": info["url"],
            }

    return await loop.run_in_executor(None, _extract)


async def stream_pcm(url: str, start_s: float = 0.0) -> AsyncIterator[bytes]:
    """Async iterator over 16 kHz mono int16 PCM chunks from a media URL.

    Spawns ffmpeg as a subprocess and yields its stdout in CHUNK_BYTES chunks.
    `start_s` seeks into the source (for resume-after-pause). The subprocess
    is terminated on iterator close or cancellation.
    """
    args = ["ffmpeg", "-loglevel", "error"]
    if start_s > 0:
        # -ss before -i is the fast input-seek; less accurate but cheap.
        args += ["-ss", f"{start_s:.2f}"]
    args += ["-i", url, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
