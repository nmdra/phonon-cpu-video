import hashlib
import json
import shutil
import struct
import subprocess
import sys
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from app.conversion import MediaError, _input_args, _run, prepare_audio, prepared_audio
from app.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(scratch_dir=tmp_path / "scratch")


@pytest.fixture
def media(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools required for synthetic fixtures")

    def create(extension="wav", rate=44100, duration=0.3):
        path = tmp_path / f"source-{rate}.{extension}"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate={rate}:duration={duration}",
                "-ac",
                "2",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        return path

    return create


@pytest.mark.parametrize(
    "extension", ["wav", "mp3", "flac", "ogg", "mp4", "mov", "mkv", "webm", "avi"]
)
@pytest.mark.parametrize("rate", [8000, 48000])
def test_formats_and_cleanup(media, settings, extension, rate):
    source = media(extension, rate)
    digest = hashlib.sha256(source.read_bytes()).digest()
    with prepared_audio(source, settings) as audio:
        assert audio != source
        assert audio.parent.stat().st_mode & 0o777 == 0o700
        with wave.open(str(audio)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            assert 0 < wav.getnframes() <= 16000
    assert not audio.exists()
    assert list(settings.scratch_dir.iterdir()) == []
    assert hashlib.sha256(source.read_bytes()).digest() == digest


def test_cleanup_on_caller_failure(media, settings):
    with pytest.raises(RuntimeError):
        with prepared_audio(media(), settings) as audio:
            raise RuntimeError("caller failed")
    assert not audio.exists()
    assert list(settings.scratch_dir.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    [
        b"garbage",
        b"#EXTM3U\nhttp://127.0.0.1:1/audio\n",
        b"ffconcat version 1.0\nfile /etc/passwd\n",
        b"RIFF\xff\xff\xff\xffWAVE",
        b"ID3broken",
        b"",
    ],
)
def test_invalid_and_playlists(tmp_path, settings, content):
    source = tmp_path / "deceptive.mp4"
    source.write_bytes(content)
    with pytest.raises(MediaError):
        with prepared_audio(source, settings):
            pytest.fail("invalid input accepted")
    assert list(settings.scratch_dir.iterdir()) == []


def test_missing_audio(tmp_path, settings):
    source = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=0.2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    with pytest.raises(MediaError, match="audio"):
        with prepared_audio(source, settings):
            pass
    assert list(settings.scratch_dir.iterdir()) == []


def test_duration_and_input_limit(media, settings):
    source = media(duration=1)
    for limited in [replace(settings, max_duration=0.1), replace(settings, max_upload_bytes=8)]:
        with pytest.raises(MediaError) as error:
            with prepared_audio(source, limited):
                pass
        assert error.value.status_code == 413
        assert list(settings.scratch_dir.iterdir()) == []


def test_output_cap_independent_of_probe(media, settings, monkeypatch):
    import app.conversion as conversion

    original = conversion._run

    def run(args, timeout, limit, output=None):
        if args[0] == "ffprobe":
            return json.dumps(
                {"format": {"duration": "0.01"}, "streams": [{"index": 0, "codec_type": "audio"}]}
            ).encode()
        return original(args, timeout, limit, output)

    monkeypatch.setattr(conversion, "_run", run)
    with pytest.raises(MediaError, match="output exceeds"):
        with prepared_audio(media(duration=1), replace(settings, max_duration=0.1)):
            pass
    assert list(settings.scratch_dir.iterdir()) == []


@pytest.mark.parametrize("duration", ["nan", "inf", "-1", None])
def test_unknown_duration(media, settings, monkeypatch, duration):
    monkeypatch.setattr(
        "app.conversion._run",
        lambda *a, **kw: json.dumps(
            {"format": {"duration": duration}, "streams": [{"index": 0, "codec_type": "audio"}]}
        ).encode(),
    )
    with pytest.raises(MediaError, match="duration"):
        with prepared_audio(media(), settings):
            pass


def test_external_mov_reference(media, settings):
    source = media("mov")
    data = source.read_bytes()
    # Real MOV fixture, altered from a self-contained url entry to external.
    needle = struct.pack(">I4sI", 12, b"url ", 1)
    assert needle in data
    source.write_bytes(data.replace(needle, struct.pack(">I4sI", 12, b"url ", 0)))
    with pytest.raises(MediaError, match="External MOV"):
        with prepared_audio(source, settings):
            pass
    assert list(settings.scratch_dir.iterdir()) == []


def test_process_limits_and_bounded_logs():
    with pytest.raises(MediaError, match="timed out") as error:
        _run([sys.executable, "-c", "import time; time.sleep(5)"], 0.05, 100)
    assert error.value.status_code == 504
    with pytest.raises(MediaError, match="metadata exceeds"):
        _run([sys.executable, "-c", 'print("a"*10000)'], 5, 100)
    assert (
        _run(
            [sys.executable, "-c", 'import sys; sys.stderr.write("x"*1000000); print("ok")'], 5, 100
        )
        == b"ok\n"
    )


def test_security_arguments():
    args = _input_args(Path("/tmp/private/input"), "mov")
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args[args.index("-format_whitelist") + 1] == "mov"
    assert args[args.index("-f") + 1] == "mov"
    assert args[args.index("-enable_drefs") + 1] == "0"
    assert args[args.index("-use_absolute_path") + 1] == "0"


def test_prepare_audio_destination(media, settings, tmp_path):
    source = media()
    destination = tmp_path / "normalized.wav"
    assert prepare_audio(source, destination, settings) == destination
    assert destination.is_file()
    with pytest.raises(FileExistsError):
        prepare_audio(source, destination, settings)
    with pytest.raises(MediaError, match="different"):
        prepare_audio(source, source, settings)
    assert list(settings.scratch_dir.iterdir()) == []
