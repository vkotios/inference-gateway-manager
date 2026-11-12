"""Bounded audio decoding for private transcription requests."""

import asyncio
import wave

from fastapi import HTTPException

from .core import finish_cleanup

# The upload is untrusted and has no extension, so ffmpeg chooses the demuxer by probing its
# contents. Without limits a crafted playlist or script (HLS, concat, SDP) can make ffmpeg open
# network addresses or other local files on the gateway's behalf. Allow only local files and
# real audio/video containers. Names are ffmpeg demuxer names (for example "mov" covers the
# MP4/M4A/3GP family and "matroska" covers MKA and WebM).
ALLOWED_PROTOCOLS = "file,pipe"
ALLOWED_FORMATS = "wav,mp3,flac,ogg,mov,mp4,m4a,matroska,webm,aac,aiff,amr,asf,ac3,caf,w64,wv"


async def decode_audio(source, target, max_seconds=7200):
    """Decode to mono PCM while enforcing the decoded byte limit, not metadata."""
    process = await asyncio.create_subprocess_exec(
        "prlimit",
        "--as=2147483648",
        "--cpu=600",
        "--",
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-protocol_whitelist",
        ALLOWED_PROTOCOLS,
        "-format_whitelist",
        ALLOWED_FORMATS,
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    size = 0
    try:
        with wave.open(str(target), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > max_seconds * 32000:
                    raise HTTPException(413, "Audio exceeds the maximum decoded duration")
                out.writeframesraw(chunk)
        if await process.wait() != 0 or size == 0:
            raise HTTPException(400, "Invalid or empty audio")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await finish_cleanup(asyncio.create_task(process.wait()))
