"""Local-only, bounded FFmpeg normalization. This is not an OS sandbox."""

import json
import math
import os
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .settings import Settings


class MediaError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _mov_references(path: Path) -> None:
    """Reject non-self-contained data references before invoking a decoder."""
    size = path.stat().st_size
    containers = {b"moov", b"trak", b"mdia", b"minf", b"dinf"}
    with path.open("rb") as stream:

        def walk(start: int, end: int, depth: int = 0) -> None:
            if depth > 12:
                raise MediaError("Invalid MOV atom nesting")
            offset = start
            while offset < end:
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    raise MediaError("Malformed MOV atom")
                length, kind = struct.unpack(">I4s", header)
                header_size = 8
                if length == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        raise MediaError("Malformed MOV atom")
                    length = struct.unpack(">Q", extended)[0]
                    header_size = 16
                if length == 0:
                    length = end - offset
                if length < header_size or offset + length > end:
                    raise MediaError("Malformed MOV atom size")
                payload = offset + header_size
                if kind in {b"rmra", b"rmda"}:
                    raise MediaError("External MOV references are not allowed")
                if kind in containers:
                    walk(payload, offset + length, depth + 1)
                elif kind == b"dref":
                    stream.seek(payload)
                    data = stream.read(8)
                    if len(data) != 8:
                        raise MediaError("Malformed MOV data references")
                    count = int.from_bytes(data[4:], "big")
                    pos = payload + 8
                    for _ in range(count):
                        if pos + 12 > offset + length:
                            raise MediaError("Malformed MOV data references")
                        stream.seek(pos)
                        entry = stream.read(12)
                        entry_size, entry_kind, flags = struct.unpack(">I4sI", entry)
                        if entry_size != 12 or entry_kind != b"url " or flags != 1:
                            raise MediaError("External MOV references are not allowed")
                        pos += entry_size
                    if pos != offset + length:
                        raise MediaError("Malformed MOV data references")
                offset += length

        walk(0, size)


def _demuxer(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(32)
    if header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE":
        return "wav"
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "avi"
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 255 and header[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    if header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free"}:
        _mov_references(path)
        return "mov"
    raise MediaError("Unsupported media container")


def _input_args(path: Path, demuxer: str) -> list[str]:
    args = ["-protocol_whitelist", "file", "-format_whitelist", demuxer, "-f", demuxer]
    if demuxer == "mov":
        args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return args + ["-i", str(path)]


def _run(args: list[str], timeout: float, limit: int, output=None) -> bytes:
    """Drain both pipes, cap retained stderr, and kill the process group on failure."""
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise MediaError("Media tools could not be started", 503) from exc
    assert process.stdout is not None and process.stderr is not None
    captured = bytearray()
    logs = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError("Media processing timed out", 504)
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "err":
                        logs.extend(chunk[: max(0, 8192 - len(logs))])
                    else:
                        total += len(chunk)
                        if total > limit:
                            raise MediaError(
                                "Decoded output exceeds limit"
                                if output
                                else "Media metadata exceeds limit",
                                413,
                            )
                        if output is not None:
                            output.write(chunk)
                        else:
                            captured.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError("Media processing timed out", 504)
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise MediaError("Media processing timed out", 504) from exc
            if code:
                # Never return tool logs, which can contain private paths or metadata.
                raise MediaError("Invalid or unsupported media")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()
    return bytes(captured)


def _convert(source: Path, destination: Path, settings: Settings) -> Path:
    demuxer = _demuxer(source)
    metadata = _run(
        [
            "ffprobe",
            "-v",
            "error",
            *_input_args(source, demuxer),
            "-show_entries",
            "format=duration:stream=index,codec_type",
            "-of",
            "json",
        ],
        settings.probe_timeout,
        256 * 1024,
    )
    try:
        info = json.loads(metadata)
        duration = float(info["format"]["duration"])
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        index = int(audio["index"])
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise MediaError("Media must have audio and a known finite duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("Media must have a known finite duration")
    if duration > settings.max_duration:
        raise MediaError("Media duration exceeds limit", 413)
    # Stream raw PCM into Python so that the output cap is enforced independently
    # of container duration and WAV sizes. Python writes a seekable WAV header.
    with destination.open("wb") as target:
        with wave.open(target, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)

            class Sink:
                def write(self, data):
                    wav.writeframesraw(data)

            _run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-xerror",
                    *_input_args(source, demuxer),
                    "-map",
                    f"0:{index}",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                settings.conversion_timeout,
                math.ceil(settings.max_duration * 32000),
                Sink(),
            )
    if destination.stat().st_size <= 44:
        raise MediaError("Media has no decodable audio")
    return destination


@contextmanager
def prepared_audio(source: Path, settings: Settings) -> Iterator[Path]:
    """Yield normalized WAV, and remove the private input and output on every exit."""
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="request-", dir=settings.scratch_dir) as directory:
        root = Path(directory)
        private_source = root / "input"
        # Open once and reject non-regular files. Copy with an independent byte cap.
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as src, private_source.open("wb") as dst:
                if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                    raise MediaError("Input must be a regular local file")
                total = 0
                while chunk := src.read(65536):
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        raise MediaError("Input exceeds byte limit", 413)
                    dst.write(chunk)
        except OSError as exc:
            raise MediaError("Cannot read local media") from exc
        yield _convert(private_source, root / "audio.wav", settings)


def prepare_audio(source: Path, destination: Path, settings: Settings) -> Path:
    """Write a caller-owned WAV. Remove incomplete output, never modify the source."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise MediaError("Input and output must be different paths")
    # Exclusive creation prevents overwriting a caller's existing file.
    with prepared_audio(source, settings) as audio:
        try:
            with destination.open("xb") as dst, audio.open("rb") as src:
                while chunk := src.read(65536):
                    dst.write(chunk)
        except FileExistsError:
            raise
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination
