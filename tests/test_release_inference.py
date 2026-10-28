"""Release helper security tests. No network, containers, or model downloads."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_inference", Path(__file__).parents[1] / "scripts" / "release-inference.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/model",
        "file:///tmp/model",
        "https:///model",
        "https://user:secret@example.com/model",
        "https://example.com/model#part",
        "https://example.com/\nmodel",
    ],
)
def test_reject_unsafe_download_url(url):
    with pytest.raises(ValueError):
        release.validate_https(url)


def test_https_url_and_hash():
    assert release.validate_https("https://example.com/model?signature=123")
    assert release.validate_sha256("a" * 64) == "a" * 64
    for digest in ("", "a" * 63, "A" * 64, "x" * 64):
        with pytest.raises(ValueError):
            release.validate_sha256(digest)


@pytest.mark.parametrize("version", ["v0.2.0", "v1.2.3", "v12.0.100"])
def test_version(version):
    assert release.validate_version(version) == version


@pytest.mark.parametrize(
    "version",
    [
        "0.2.0",
        "v01.2.0",
        "v1.02.0",
        "v1.2.03",
        "v1.2",
        "v1.2.3-rc1",
        "v1.2.3\n",
        "--help",
        "v1.2.3;id",
    ],
)
def test_reject_invalid_version(version):
    with pytest.raises(ValueError):
        release.validate_version(version)


def archive(tmp_path, members):
    path = tmp_path / "model.tar"
    with tarfile.open(path, "w") as target:
        for name, kind, body in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(body)
            else:
                info.linkname = body.decode()
            target.addfile(info, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return path


@pytest.mark.parametrize(
    ("name", "kind", "body"),
    [
        ("../escape", tarfile.REGTYPE, b"x"),
        ("/absolute", tarfile.REGTYPE, b"x"),
        ("cache/../../escape", tarfile.REGTYPE, b"x"),
        ("cache\\..\\escape", tarfile.REGTYPE, b"x"),
        ("cache/link", tarfile.SYMTYPE, b"../../escape"),
        ("cache/link", tarfile.LNKTYPE, b"cache/file"),
        ("cache/fifo", tarfile.FIFOTYPE, b""),
        ("cache/device", tarfile.CHRTYPE, b""),
    ],
)
def test_reject_unsafe_archive_before_extraction(tmp_path, name, kind, body):
    source = archive(tmp_path, [("safe", tarfile.REGTYPE, b"safe"), (name, kind, body)])
    destination = tmp_path / "cache"
    with pytest.raises(ValueError, match="Unsafe"):
        release.extract_model(source, destination)
    assert not (destination / "safe").exists()
    assert not (tmp_path / "escape").exists()


def test_archive_regular_data_and_permissions(tmp_path):
    source = archive(tmp_path, [("hub/model/weights", tarfile.REGTYPE, b"weights")])
    destination = tmp_path / "cache"
    release.extract_model(source, destination)
    weights = destination / "hub/model/weights"
    assert weights.read_bytes() == b"weights"
    assert weights.stat().st_mode & 0o777 == 0o666


def test_archive_budget_and_duplicates(tmp_path):
    source = archive(tmp_path, [("weights", tarfile.REGTYPE, b"too large")])
    with pytest.raises(ValueError, match="budget"):
        release.extract_model(source, tmp_path / "small", limit=1)
    source = archive(tmp_path, [("weights", tarfile.REGTYPE, b"a")] * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        release.extract_model(source, tmp_path / "duplicate")


@pytest.mark.parametrize(
    "output",
    [
        "Hello, WORLD! Some extra words.",
        '{"text": "Hello world."}',
        '{"transcription": "HELLO, world!"}',
    ],
)
def test_transcript_normalized_contiguous_words(output):
    release.assert_transcript(output, "hello world")


@pytest.mark.parametrize(
    "output",
    [
        "hello other world",
        "hello",
        '{"error": "hello world"}',
        '["hello world"]',
    ],
)
def test_transcript_must_match_real_text(output):
    with pytest.raises(ValueError):
        release.assert_transcript(output, "hello world")


def test_transcript_empty_expectation_rejected():
    with pytest.raises(ValueError):
        release.assert_transcript("hello world", "!!")


def test_download_hash_size_and_partial_cleanup(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        url = "https://example.com/model"

    def opener():
        return SimpleNamespace(open=lambda *args, **kwargs: Response(b"model"))

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *args: opener())
    target = tmp_path / "model"
    digest = hashlib.sha256(b"model").hexdigest()
    release.download(Response.url, digest, target, 5)
    assert target.read_bytes() == b"model"
    target.unlink()
    with pytest.raises(ValueError, match="mismatch"):
        release.download(Response.url, "0" * 64, target, 5)
    assert not target.exists()
    with pytest.raises(ValueError, match="budget"):
        release.download(Response.url, digest, target, 4)
    assert not target.exists()


@pytest.mark.parametrize("exit_code", [0, 137])
def test_runtime_video_and_shutdown_checks(tmp_path, monkeypatch, exit_code):
    calls = []
    fixture = tmp_path / "fixture.wav"
    fixture.write_bytes(b"speech")
    cache = tmp_path / "cache"
    cache.mkdir()
    evidence = tmp_path / "evidence.json"

    def run(*args, **kwargs):
        calls.append(args)
        if "ffprobe" in args:
            return '{"streams": [{"codec_type": "audio"}, {"codec_type": "video"}]}'
        if "inspect" in args:
            return f'{{"OOMKilled": false, "ExitCode": {exit_code}}}'
        if "transcribe" in args or ("exec" in args and "-e" in args):
            return '{"text": "hello world"}'
        return ""

    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    if exit_code == 137:
        with pytest.raises(RuntimeError, match="gracefully"):
            release.runtime("image@sha256:abc", fixture, cache, "hello world", "model", evidence)
    else:
        release.runtime("image@sha256:abc", fixture, cache, "hello world", "model", evidence)
        assert '"ExitCode": 0' in evidence.read_text()
    video = next(call for call in calls if "ffmpeg" in call and "libx264" in call)
    assert "-shortest" in video and "1:v:0" in video and "0:a:0" in video
    detached = next(call for call in calls if "--detach" in call)
    assert "--rm" not in detached
    assert any("ffprobe" in call for call in calls)


def test_redirect_cannot_downgrade_https():
    with pytest.raises(ValueError):
        release.HTTPSOnly().redirect_request(None, None, 302, "", {}, "http://example.com")
