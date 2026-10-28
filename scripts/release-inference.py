#!/usr/bin/env python3
"""Manual release acceptance. Downloads occur only when invoked, never on import.

The model tar must contain the complete /home/phonon/.cache tree contents.
Both inference paths run without network access, so missing model files fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

MAX_FIXTURE = 100 * 1024**2
MAX_MODEL = 16 * 1024**3
MAX_UNPACKED = 24 * 1024**3
MAX_MEMBERS = 100_000


def validate_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Download URL must use HTTPS without credentials")
    if parsed.fragment or any(ord(char) < 33 for char in url):
        raise ValueError("Invalid download URL")
    return url


def validate_sha256(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected lowercase SHA-256 hex digest")
    return value


def validate_version(version: str) -> str:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Version must be an exact vX.Y.Z tag without leading zeroes")
    return version


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, digest: str, target: Path, limit: int) -> None:
    validate_https(url)
    validate_sha256(digest)
    opener = urllib.request.build_opener(HTTPSOnly())
    checksum = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 600
    try:
        with opener.open(url, timeout=30) as response, target.open("xb") as output:
            validate_https(response.url)
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    raise ValueError("Download exceeded byte or time budget")
                checksum.update(chunk)
                output.write(chunk)
        if checksum.hexdigest() != digest:
            raise ValueError("Downloaded file SHA-256 mismatch")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def extract_model(archive: Path, destination: Path, *, limit: int = MAX_UNPACKED) -> None:
    """Validate all metadata before extraction; never accept links or special files."""
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    with tarfile.open(archive, "r:*") as source:
        members = []
        names = set()
        size = 0
        for member in source:
            path = PurePosixPath(member.name)
            if member.name in (".", "./") and member.isdir():
                continue
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not path.parts
                or not (member.isdir() or member.isfile())
                or member.issym()
                or member.islnk()
                or member.size < 0
            ):
                raise ValueError("Unsafe model archive member")
            if path in names:
                raise ValueError("Duplicate model archive member")
            names.add(path)
            size += member.size
            if size > limit or len(members) >= MAX_MEMBERS:
                raise ValueError("Model archive exceeds extraction budget")
            members.append(member)
        # Python 3.12 data filter adds a second independent traversal/type check.
        source.extractall(destination, members=members, filter="data")
    # Cache is disposable, shared with container uid 1001, and never executable.
    destination.chmod(0o777)
    for path in destination.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)


def normalized_words(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())


def assert_transcript(output: str, expected: str) -> None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = output
    if isinstance(payload, dict):
        payload = payload.get("text", payload.get("transcription"))
    if not isinstance(payload, str):
        raise ValueError("Transcription did not contain a text string")
    actual_words, expected_words = normalized_words(payload), normalized_words(expected)
    if not expected_words or not any(
        actual_words[i : i + len(expected_words)] == expected_words
        for i in range(len(actual_words) - len(expected_words) + 1)
    ):
        raise ValueError("Transcription did not contain the expected normalized word sequence")


def run(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, timeout=timeout
    ).stdout


def runtime(
    image: str, fixture: Path, cache: Path, expected: str, model: str, evidence: Path
) -> None:
    """Exercise real WAV/video CLI and HTTP, offline cache reuse and cleanup."""
    common = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--mount",
        f"type=bind,src={cache},dst=/home/phonon/.cache",
        "--mount",
        f"type=bind,src={fixture.parent},dst=/fixtures,readonly",
        "-e",
        "HF_HUB_OFFLINE=1",
        "-e",
        "TRANSFORMERS_OFFLINE=1",
        "-e",
        "HF_DATASETS_OFFLINE=1",
    ]

    def isolated(*args: str, timeout: int = 120) -> str:
        container = f"phonon-check-{secrets.token_hex(8)}"
        try:
            return run(*common, "--name", container, *args, timeout=timeout)
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container], capture_output=True, timeout=30, check=False
            )

    generated = fixture.parent / "generated"
    generated.mkdir(mode=0o777)
    generated.chmod(0o777)
    # Build speech-bearing WAV and MP4 fixtures in the candidate's own FFmpeg.
    for name, flags in [
        ("speech.wav", ["-ac", "1", "-ar", "16000"]),
        (
            "speech.mp4",
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:r=1",
                "-map",
                "1:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
            ],
        ),
    ]:
        isolated(
            "--mount",
            f"type=bind,src={generated},dst=/generated",
            "--entrypoint",
            "ffmpeg",
            image,
            "-nostdin",
            "-v",
            "error",
            "-i",
            f"/fixtures/{fixture.name}",
            *flags,
            f"/generated/{name}",
        )
    streams = json.loads(
        isolated(
            "--entrypoint",
            "ffprobe",
            image,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            "/fixtures/generated/speech.mp4",
        )
    )
    if {stream["codec_type"] for stream in streams["streams"]} != {"audio", "video"}:
        raise ValueError("Acceptance MP4 must contain both audio and video")
    transcripts = {}
    shutdown = {}
    for name in ("speech.wav", "speech.mp4"):
        output = isolated(image, "transcribe", model, f"/fixtures/generated/{name}", timeout=1200)
        assert_transcript(output, expected)
        transcripts[f"cli-{name}"] = output

    name = f"phonon-release-{secrets.token_hex(8)}"
    key = secrets.token_urlsafe(32)
    # No published ports: the HTTP client runs inside the network-isolated container.
    run(
        *common[:2],
        "--detach",
        *common[3:],  # Keep detached container after stop so exit state can be inspected.
        "--name",
        name,
        "-e",
        f"PHONON_VIDEO_API_KEY={key}",
        image,
        "serve-video",
        model,
    )
    try:
        ready = False
        deadline = time.monotonic() + 1000
        while time.monotonic() < deadline:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen("
                    "'http://127.0.0.1:8000/health', timeout=3).read()",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(2)
        if not ready:
            raise RuntimeError("HTTP service failed its startup deadline")
        for media in ("speech.wav", "speech.mp4"):
            client = r"""
import os, pathlib, urllib.request
boundary = "phonon-release-multipart"
model = os.environ["RELEASE_MODEL"]
media = os.environ["RELEASE_MEDIA"]
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
        f"{model}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{media}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
body += pathlib.Path("/fixtures/generated", media).read_bytes()
body += f"\r\n--{boundary}--\r\n".encode()
request = urllib.request.Request("http://127.0.0.1:8000/v1/audio/transcriptions", body,
    {"Authorization": "Bearer " + os.environ["PHONON_VIDEO_API_KEY"],
     "Content-Type": "multipart/form-data; boundary=" + boundary})
with urllib.request.urlopen(request, timeout=920) as response:
    result = response.read(4 * 1024 * 1024 + 1)
    if len(result) > 4 * 1024 * 1024:
        raise ValueError("Response too large")
    print(result.decode())
"""
            output = run(
                "docker",
                "exec",
                "-e",
                f"RELEASE_MODEL={model}",
                "-e",
                f"RELEASE_MEDIA={media}",
                name,
                "python",
                "-c",
                client,
                timeout=950,
            )
            assert_transcript(output, expected)
            transcripts[f"http-{media}"] = output
        # No scratch files remain after completed inference.
        run(
            "docker",
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path; "
            "assert not list(Path('/tmp/phonon-video').glob('request-*')), 'scratch leak'",
        )
    finally:
        logs = subprocess.run(
            ["docker", "logs", name], text=True, capture_output=True, timeout=30, check=False
        )
        evidence.with_suffix(".log").write_text(logs.stdout + logs.stderr)
        try:
            run("docker", "stop", "--time", "15", name, timeout=30)
            shutdown = json.loads(
                run(
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    name,
                )
            )
            if shutdown["OOMKilled"] or shutdown["ExitCode"] not in (0, 143):
                raise RuntimeError("Service did not stop gracefully")
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True, timeout=30, check=False
            )
    evidence.write_text(
        json.dumps(
            {
                "image": image,
                "model": model,
                "transcripts": transcripts,
                "shutdown": shutdown,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--fixture-url", required=True)
    parser.add_argument("--fixture-sha256", required=True)
    parser.add_argument("--model-url", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--model", default="phonon-2.5")
    parser.add_argument("--expected-words", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not normalized_words(args.expected_words):
        parser.error("Expected words must not be empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", args.model):
        parser.error("Invalid model identifier")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-inference-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        fixture, archive, cache = root / "speech.media", root / "model.tar", root / "cache"
        download(args.fixture_url, args.fixture_sha256, fixture, MAX_FIXTURE)
        download(args.model_url, args.model_sha256, archive, MAX_MODEL)
        extract_model(archive, cache)
        runtime(args.image, fixture, cache, args.expected_words, args.model, args.evidence)
        record = json.loads(args.evidence.read_text())
        record.update(
            fixture_url=args.fixture_url,
            fixture_sha256=args.fixture_sha256,
            model_url=args.model_url,
            model_sha256=args.model_sha256,
            expected_words=args.expected_words,
            budgets={
                "fixture_bytes": MAX_FIXTURE,
                "model_bytes": MAX_MODEL,
                "unpacked_bytes": MAX_UNPACKED,
                "download_seconds": 600,
                "cli_seconds": 1200,
                "http_seconds": 950,
            },
        )
        args.evidence.write_text(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
