"""No-model CLI checks with a fake upstream executable and local media."""

import json
import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from app import cli
from app.settings import Settings


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source with spaces.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0" * 6400)
    return path


@pytest.fixture
def fake_upstream(tmp_path):
    path = tmp_path / "upstream"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "print(json.dumps(sys.argv[1:]), flush=True)\n"
        "print('upstream diagnostic', file=sys.stderr, flush=True)\n"
        "if os.environ.get('WAIT_FOR_SIGNAL'):\n"
        "    pathlib.Path(os.environ['READY_FILE']).write_text(sys.argv[3])\n"
        "    time.sleep(60)\n"
        "sys.exit(int(os.environ.get('UPSTREAM_EXIT', '0')))\n"
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def configured(monkeypatch, tmp_path, fake_upstream):
    settings = Settings(scratch_dir=tmp_path / "scratch", scratch_budget_bytes=256 * 1024**2)
    monkeypatch.setattr(cli, "UPSTREAM_ENTRYPOINT", str(fake_upstream))
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    return settings


def test_json_arguments_status_and_cleanup(source, configured, monkeypatch, capfd):
    original = source.read_bytes()
    monkeypatch.setenv("UPSTREAM_EXIT", "7")
    assert (
        cli.main(
            [
                "transcribe",
                "some/model",
                str(source),
                "--response-format",
                "json",
                "--hotwords",
                "two words",
            ]
        )
        == 7
    )
    stdout, stderr = capfd.readouterr()
    args = json.loads(stdout)
    assert args[:2] == ["transcribe", "some/model"]
    assert args[3:] == ["--response-format", "json", "--hotwords", "two words"]
    assert args[2] != str(source)
    assert not Path(args[2]).exists()
    assert source.read_bytes() == original
    assert stderr == "upstream diagnostic\n"
    assert not list(configured.scratch_dir.glob("**/*.wav"))


def test_invalid_media_is_error_without_stdout(tmp_path, configured, capfd):
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a media file")
    assert cli.main(["transcribe", "model", str(bad)]) == 2
    stdout, stderr = capfd.readouterr()
    assert stdout == ""
    assert "Media error:" in stderr


def test_missing_upstream_cleans_scratch(source, configured, monkeypatch, capfd):
    monkeypatch.setattr(cli, "UPSTREAM_ENTRYPOINT", "/does/not/exist")
    assert cli.main(["transcribe", "model", str(source)]) == 1
    assert not list(configured.scratch_dir.glob("**/*.wav"))
    assert "phonon-video:" in capfd.readouterr().err


@pytest.mark.parametrize(
    "arguments", [[], ["--help"], ["serve", "model", "--port", "9000"], ["version"]]
)
def test_native_commands_exec_unchanged(arguments, monkeypatch):
    class ExecCalled(Exception):
        pass

    def fake_exec(path, argv):
        assert path == cli.UPSTREAM_ENTRYPOINT
        assert argv == [path, *arguments]
        raise ExecCalled

    monkeypatch.setattr(cli.os, "execv", fake_exec)
    with pytest.raises(ExecCalled):
        cli.main(arguments)


def test_transcribe_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["transcribe", "--help"])
    assert exc.value.code == 0
    assert "MODEL FILE" in capsys.readouterr().out


def test_serve_needs_no_key_by_default(monkeypatch, configured):
    from app import api

    monkeypatch.delenv("PHONON_VIDEO_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(api, "run_server", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["serve-video", "model"]) == 0
    assert calls[0]["api_key"] is None
    assert calls[0]["threads"] is None


def test_serve_contract(monkeypatch, configured):
    from app import api

    calls = []
    monkeypatch.setattr(api, "run_server", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setenv("PHONON_VIDEO_API_KEY", "secret")
    assert (
        cli.main(
            ["serve-video", "model", "--host", "127.0.0.1", "--port", "9000", "--threads", "2"]
        )
        == 0
    )
    assert calls == [
        {
            "model": "model",
            "host": "127.0.0.1",
            "port": 9000,
            "threads": 2,
            "api_key": "secret",
            "settings": configured,
        }
    ]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_forwarding_and_cleanup(source, fake_upstream, tmp_path, signum):
    ready = tmp_path / "ready"
    env = os.environ.copy()
    env.update(
        {
            "WAIT_FOR_SIGNAL": "1",
            "READY_FILE": str(ready),
            "PHONON_VIDEO_SCRATCH_DIR": str(tmp_path / "scratch"),
            "PHONON_VIDEO_SCRATCH_BUDGET_BYTES": str(256 * 1024**2),
        }
    )
    code = (
        "from app import cli; import sys; "
        f"cli.UPSTREAM_ENTRYPOINT={str(fake_upstream)!r}; "
        "sys.exit(cli.main(sys.argv[1:]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, "transcribe", "model", str(source)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"wrapper exited early: {process.communicate()!r}")
            time.sleep(0.05)
        assert ready.exists(), "upstream did not start"
        normalized = Path(ready.read_text())
        assert normalized.exists()
        process.send_signal(signum)
        process.communicate(timeout=10)
        assert process.returncode == 128 + signum
        assert not normalized.exists()
        assert source.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_signal_during_preparation_cleans_scratch(monkeypatch, tmp_path, configured):
    from contextlib import contextmanager

    scratch = tmp_path / "partial.wav"

    @contextmanager
    def interrupted_preparation(source, settings):
        scratch.write_bytes(b"partial")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            yield scratch
        finally:
            scratch.unlink()

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "prepared_audio", interrupted_preparation)
    assert cli.main(["transcribe", "model", "source.wav"]) == 143
    assert not scratch.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_cleanup_kills_descendant_after_direct_child_exits(
    source,
    configured,
    fake_upstream,
    monkeypatch,
    tmp_path,
):
    marker = tmp_path / "descendant"
    fake_upstream.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time, pathlib\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "else:\n"
        f"    while not pathlib.Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
        "    raise SystemExit(7)\n"
    )
    descendant = None
    try:
        assert cli.main(["transcribe", "model", str(source)]) == 7
        descendant = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{descendant}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("Descendant survived CLI cleanup")
        assert not list(configured.scratch_dir.glob("**/*.wav"))
    finally:
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
