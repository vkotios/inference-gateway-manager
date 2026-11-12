"""Audio decoding must only open the uploaded local file, and only as a real audio container."""

import asyncio
import shutil
import socket
import subprocess
import threading

import pytest
from fastapi import HTTPException

from gateway import aux_audio
from gateway.aux_audio import decode_audio

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("prlimit")), reason="ffmpeg and prlimit required"
)


async def test_ffmpeg_is_started_with_protocol_and_format_allowlists(monkeypatch, tmp_path):
    """Version-independent: the restriction must be on the command line, before the input."""
    seen = {}

    class Stdout:
        async def read(self, _size):
            return b""

    class Process:
        stdout = Stdout()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(HTTPException) as empty:
        await decode_audio(tmp_path / "source", tmp_path / "audio.wav")
    assert empty.value.status_code == 400  # no decoded bytes: the normal "invalid audio" path
    argv = seen["argv"]
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    allowed = argv[argv.index("-format_whitelist") + 1].split(",")
    assert argv.index("-protocol_whitelist") < argv.index("-i")
    assert argv.index("-format_whitelist") < argv.index("-i")
    for container in ("hls", "concat", "sdp", "rtsp", "http", "tcp", "udp", "image2", "lavfi"):
        assert container not in allowed
    assert aux_audio.ALLOWED_PROTOCOLS == "file,pipe"
    for needed in ("wav", "mp3", "flac", "ogg", "mov", "matroska", "webm", "aac"):
        assert needed in allowed


def make_sample(tmp_path, name, ext, encoder):
    """Create one second of a 440 Hz tone; skip when this ffmpeg lacks the encoder."""
    path = tmp_path / f"{name}.{ext}"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        + encoder
        + [str(path)],
        capture_output=True,
    )
    if result.returncode != 0 or not path.exists():
        pytest.skip(f"this ffmpeg cannot encode {name}")
    return path


@needs_ffmpeg
@pytest.mark.parametrize(
    "name,ext,encoder",
    [
        ("wav", "wav", ["-c:a", "pcm_s16le"]),
        ("mp3", "mp3", ["-c:a", "libmp3lame"]),
        ("flac", "flac", ["-c:a", "flac"]),
        ("ogg", "ogg", ["-c:a", "libvorbis"]),
        ("opus", "ogg", ["-c:a", "libopus"]),
        ("m4a", "m4a", ["-c:a", "aac"]),
        ("mp4", "mp4", ["-c:a", "aac"]),
        ("webm", "webm", ["-c:a", "libopus"]),
        ("mka", "mka", ["-c:a", "flac"]),
        ("aac", "aac", ["-c:a", "aac"]),
        ("aiff", "aiff", ["-c:a", "pcm_s16be"]),
        ("ac3", "ac3", ["-c:a", "ac3"]),
    ],
)
async def test_common_audio_containers_still_decode_as_an_extensionless_upload(
    tmp_path, name, ext, encoder
):
    sample = make_sample(tmp_path, name, ext, encoder)
    upload = tmp_path / "source"  # the gateway stores uploads without an extension
    shutil.copy(sample, upload)
    await decode_audio(upload, tmp_path / "audio.wav")
    import wave

    with wave.open(str(tmp_path / "audio.wav")) as decoded:
        assert decoded.getframerate() == 16000 and decoded.getnchannels() == 1
        assert decoded.getnframes() > 8000  # about a second of audio survived


@needs_ffmpeg
async def test_a_container_outside_the_allowlist_is_refused(tmp_path):
    """Proves the format allowlist is active: Sun AU decodes without it but is not allowed."""
    sample = make_sample(tmp_path, "au", "au", ["-c:a", "pcm_s16be"])
    upload = tmp_path / "source"
    shutil.copy(sample, upload)
    with pytest.raises(HTTPException) as refused:
        await decode_audio(upload, tmp_path / "audio.wav")
    assert refused.value.status_code == 400


@needs_ffmpeg
@pytest.mark.parametrize("shape", ["hls-network", "concat-network", "hls-local-file"])
async def test_crafted_playlists_and_scripts_open_nothing(tmp_path, shape):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(2)
    port = listener.getsockname()[1]
    connections = []

    def accept():
        try:
            while True:
                conn, _ = listener.accept()
                connections.append(conn.recv(64))
                conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    real = make_sample(tmp_path, "real", "wav", ["-c:a", "pcm_s16le"])
    bodies = {
        "hls-network": f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nhttp://127.0.0.1:{port}/x.ts\n#EXT-X-ENDLIST\n",
        "concat-network": f"ffconcat version 1.0\nfile 'http://127.0.0.1:{port}/x'\n",
        "hls-local-file": f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nfile://{real}\n#EXT-X-ENDLIST\n",
    }
    upload = tmp_path / "source"
    upload.write_text(bodies[shape])
    try:
        with pytest.raises(HTTPException) as refused:
            await decode_audio(upload, tmp_path / "audio.wav")
        assert refused.value.status_code == 400
        assert connections == []  # nothing was fetched on the gateway's behalf
    finally:
        listener.close()
