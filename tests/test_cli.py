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
@pytest.mark.parametrize("managed", [False, True])
def test_signal_forwarding_and_cleanup(source, fake_upstream, tmp_path, signum, managed):
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
        [sys.executable, "-c", code, "transcribe", "model", str(source)]
        + (["--output", str(tmp_path / "result")] if managed else []),
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
        assert not (tmp_path / "result").exists()
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


@pytest.fixture
def managed_upstream(fake_upstream, configured, monkeypatch, tmp_path):
    record = tmp_path / "arguments.json"
    monkeypatch.setenv("ARGUMENT_RECORD", str(record))
    fake_upstream.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "pathlib.Path(os.environ['ARGUMENT_RECORD']).write_text(json.dumps(sys.argv[1:]))\n"
        "print('upstream diagnostic', file=sys.stderr, flush=True)\n"
        "if os.environ.get('RACE_DEST'):\n"
        "    pathlib.Path(os.environ['RACE_DEST']).write_bytes(b'competitor')\n"
        "if os.environ.get('DESCENDANT'):\n"
        "    if os.fork() == 0: time.sleep(60); os._exit(0)\n"
        "if os.environ.get('WAIT_FOR_SIGNAL'):\n"
        "    pathlib.Path(os.environ['READY_FILE']).write_text(sys.argv[3])\n"
        "    time.sleep(60)\n"
        "if os.environ.get('BIG_OUTPUT'):\n"
        "    sys.stdout.buffer.write(b'x' * (4 * 1024 * 1024 + 1)); sys.stdout.flush()\n"
        "    time.sleep(60)\n"
        "elif os.environ.get('INVALID_UTF8'): sys.stdout.buffer.write(b'\\xff')\n"
        "elif os.environ.get('BAD_JSON'): print('not json')\n"
        "elif '--json' in sys.argv:\n"
        "    print(json.dumps({'text': 'héllo world', 'model': 'test', 'segments': ["
        "{'id': 0, 'start': 0, 'end': 1, 'text': 'héllo'},"
        "{'id': 1, 'start': 1, 'end': 2, 'text': 'world'}]}))\n"
        "else: print('héllo world')\n"
        "sys.exit(int(os.environ.get('UPSTREAM_EXIT', '0')))\n"
    )
    return record


@pytest.mark.parametrize(
    "options",
    [
        ["--output-format"],
        ["--output"],
        ["--segment-duration"],
        ["--output-format", "bad"],
        ["--segment-duration", "nan"],
        ["--segment-duration", "0"],
        ["--output", "a", "--output=b"],
        ["--output-format=txt", "--output-format=txt"],
        ["--segment-duration=1", "--segment-duration=2"],
        ["--json", "--output-format=srt"],
        ["--output="],
    ],
)
def test_export_usage_before_inference(source, managed_upstream, options, capfd):
    assert cli.main(["transcribe", "model", str(source), *options]) == 2
    assert not managed_upstream.exists()
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("format_name", ["txt", "srt", "vtt", "json"])
def test_managed_formats_and_forwarding(source, managed_upstream, format_name, capfd):
    options = ["--hotwords", "two words", "--output-format", format_name, "--threads", "2"]
    if format_name == "json":
        options += ["--json"]
    assert cli.main(["transcribe", "model", str(source), *options]) == 0
    stdout, stderr = capfd.readouterr()
    assert "upstream diagnostic" not in stdout
    assert stderr == "upstream diagnostic\n"
    forwarded = json.loads(managed_upstream.read_text())[3:]
    assert forwarded == ["--hotwords", "two words", "--threads", "2", "--json"]
    if format_name == "json":
        assert json.loads(stdout)["model"] == "test"
    elif format_name == "txt":
        assert stdout == "héllo world\n"
    elif format_name == "srt":
        assert stdout.startswith("1\n00:00:00,000 --> 00:00:01,000\nhéllo\n\n")
    else:
        assert stdout.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhéllo\n\n")


@pytest.mark.parametrize("format_name", [None, "txt", "srt", "vtt", "json", "native-json"])
def test_file_output(source, managed_upstream, tmp_path, format_name, capfd):
    destination = tmp_path / "résult with spaces.txt"
    options = ["--output", str(destination)]
    if format_name == "native-json":
        options += ["--json"]
    elif format_name:
        options += ["--output-format", format_name]
    original = source.read_bytes()
    assert cli.main(["transcribe", "model", str(source), *options]) == 0
    assert capfd.readouterr().out == ""
    content = destination.read_text()
    if format_name in {"json", "native-json"}:
        assert json.loads(content)["model"] == "test"
        assert json.loads(content)["text"] == "héllo world"
    else:
        assert "héllo" in content
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".phonon-*"))


def test_grouping_and_stdout_destination(source, managed_upstream, capfd):
    assert (
        cli.main(["transcribe", "model", str(source), "--segment-duration=30", "--output=-"]) == 0
    )
    assert capfd.readouterr().out == "héllo world\n"
    assert "--json" in json.loads(managed_upstream.read_text())


@pytest.mark.parametrize("kind", ["existing", "symlink", "source", "missing"])
def test_destination_preflight(source, managed_upstream, tmp_path, kind, capfd):
    destination = tmp_path / "output"
    if kind == "existing":
        destination.write_bytes(b"original")
    elif kind == "symlink":
        destination.symlink_to(tmp_path / "absent")
    elif kind == "source":
        destination = source
    else:
        destination = tmp_path / "missing" / "output"
    original = source.read_bytes()
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == 1
    assert not managed_upstream.exists()
    assert source.read_bytes() == original
    if kind == "existing":
        assert destination.read_bytes() == b"original"
    if kind == "symlink":
        assert destination.is_symlink()
    assert capfd.readouterr().out == ""


def test_readonly_preflight(source, managed_upstream, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, "access", lambda *args: False)
    assert cli.main(["transcribe", "model", str(source), "--output", str(tmp_path / "out")]) == 1
    assert not managed_upstream.exists()


@pytest.mark.parametrize("failure", ["BAD_JSON", "BIG_OUTPUT", "INVALID_UTF8", "UPSTREAM_EXIT"])
@pytest.mark.parametrize("file_output", [False, True])
def test_managed_failure_is_atomic(
    source,
    managed_upstream,
    tmp_path,
    monkeypatch,
    capfd,
    failure,
    file_output,
):
    monkeypatch.setenv(failure, "7" if failure == "UPSTREAM_EXIT" else "1")
    destination = tmp_path / "out"
    options = ["--output-format=json"]
    if file_output:
        options += ["--output", str(destination)]
    started = time.monotonic()
    assert cli.main(["transcribe", "model", str(source), *options]) == (
        7 if failure == "UPSTREAM_EXIT" else 1
    )
    assert time.monotonic() - started < 10
    assert capfd.readouterr().out == ""
    assert not destination.exists()
    assert not list(tmp_path.glob(".phonon-*"))


@pytest.mark.parametrize("file_output", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"text":null}',
        b'{"text":"x","nested":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}",
    ],
)
def test_native_json_output_is_validated(
    source, managed_upstream, fake_upstream, tmp_path, capfd, body, file_output
):
    fake_upstream.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({body!r})\n")
    destination = tmp_path / "out"
    output = str(destination) if file_output else "-"
    assert cli.main(["transcribe", "model", str(source), "--json", "--output", output]) == 1
    assert capfd.readouterr().out == ""
    assert not destination.exists()
    assert not list(tmp_path.glob(".phonon-*"))


def test_native_json_file_retains_exact_bytes(
    source, managed_upstream, fake_upstream, tmp_path, capfd
):
    body = b'{"text":"hello", "model": "test"}\n\n'
    fake_upstream.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({body!r})\n")
    destination = tmp_path / "out"
    arguments = ["transcribe", "model", str(source), "--json", "--output", str(destination)]
    assert cli.main(arguments) == 0
    assert destination.read_bytes() == body
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("phase", ["cleanup", "return"])
def test_late_postcommit_signal_reports_publication(
    source, managed_upstream, tmp_path, monkeypatch, capfd, phase
):
    destination = tmp_path / "out"
    if phase == "cleanup":
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            result = original_unlink(path, *args, **kwargs)
            if path.name.startswith(".phonon-"):
                os.kill(os.getpid(), signal.SIGTERM)
            return result

        monkeypatch.setattr(Path, "unlink", unlink)
    else:
        original_publish = cli._publish

        def publish(*args):
            original_publish(*args)
            os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr(cli, "_publish", publish)
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == 143
    assert destination.read_text() == "héllo world\n"
    stdout, stderr = capfd.readouterr()
    assert stdout == ""
    assert "complete output file was published" in stderr
    assert not list(tmp_path.glob(".phonon-*"))


def test_native_file_invalid_utf8(source, managed_upstream, tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("INVALID_UTF8", "1")
    destination = tmp_path / "out"
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == 1
    assert not destination.exists()
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("unsupported", [False, True])
def test_publish_no_clobber(source, managed_upstream, tmp_path, monkeypatch, unsupported):
    destination = tmp_path / "out"
    if unsupported:

        def fail_link(*args):
            raise OSError("unsupported link")

        monkeypatch.setattr(cli.os, "link", fail_link)
    else:
        monkeypatch.setenv("RACE_DEST", str(destination))
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == 1
    if unsupported:
        assert not destination.exists()
    else:
        assert destination.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".phonon-*"))


def test_postcommit_unlink_warning(source, managed_upstream, tmp_path, monkeypatch, capfd):
    destination = tmp_path / "out"
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name.startswith(".phonon-"):
            raise OSError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == 0
    assert destination.read_text() == "héllo world\n"
    stdout, stderr = capfd.readouterr()
    assert stdout == ""
    assert "cleanup warning" in stderr
    for staging in tmp_path.glob(".phonon-*"):
        original_unlink(staging)


@pytest.mark.parametrize("phase", ["render", "stage", "prelink", "postlink", "stdout"])
def test_postprocessing_signals(source, managed_upstream, tmp_path, monkeypatch, capfd, phase):
    destination = tmp_path / "out"
    if phase in {"render", "stdout"}:
        original = cli.render_transcript

        def render(*args, **kwargs):
            result = original(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return result

        monkeypatch.setattr(cli, "render_transcript", render)
    elif phase == "stage":
        original_temp = cli.tempfile.NamedTemporaryFile

        def stage(*args, **kwargs):
            result = original_temp(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return result

        monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", stage)
    elif phase == "prelink":
        original_mask = cli.signal.pthread_sigmask

        def mask(how, signals):
            if how == signal.SIG_BLOCK:
                os.kill(os.getpid(), signal.SIGTERM)
            return original_mask(how, signals)

        monkeypatch.setattr(cli.signal, "pthread_sigmask", mask)
    else:
        original_link = cli.os.link

        def link(*args):
            original_link(*args)
            os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr(cli.os, "link", link)
    options = ["--output-format=txt"]
    if phase != "stdout":
        options += ["--output", str(destination)]
    assert cli.main(["transcribe", "model", str(source), *options]) == 143
    assert destination.exists() == (phase == "postlink")
    if phase == "postlink":
        assert destination.read_text() == "héllo world\n"
    assert capfd.readouterr().out == ""
    assert not list(tmp_path.glob(".phonon-*"))


def test_managed_descendant_stdout_does_not_hang(source, managed_upstream, monkeypatch, capfd):
    monkeypatch.setenv("DESCENDANT", "1")
    started = time.monotonic()
    assert cli.main(["transcribe", "model", str(source), "--output-format=txt"]) == 0
    assert time.monotonic() - started < 10
    assert capfd.readouterr().out == "héllo world\n"


def test_capture_eof_still_supervises_running_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import os,time; os.close(1); time.sleep(60)"],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 0.3

    def check_deadline():
        if time.monotonic() >= deadline:
            raise cli._Termination(signal.SIGTERM)

    try:
        with pytest.raises(cli._Termination):
            cli._capture(child, check_deadline)
        assert child.poll() is None
    finally:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        assert child.stdout is not None
        child.stdout.close()


def test_capture_delayed_chunks():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; os.write(1,b'first'); time.sleep(.2); os.write(1,b'second')",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert cli._capture(child, lambda: None) == (0, b"firstsecond")
    finally:
        child.wait()
        assert child.stdout is not None
        child.stdout.close()


def test_rendered_output_limit(source, managed_upstream, monkeypatch, capfd):
    monkeypatch.setattr(cli, "render_transcript", lambda *args: b"x" * (cli.TRANSCRIPT_LIMIT + 1))
    assert cli.main(["transcribe", "model", str(source), "--output-format=txt"]) == 1
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("signal_during_write", [False, True])
def test_staging_write_failure_or_signal(
    source,
    managed_upstream,
    tmp_path,
    monkeypatch,
    signal_during_write,
    capfd,
):
    from contextlib import contextmanager

    original_temp = cli.tempfile.NamedTemporaryFile

    @contextmanager
    def staging(*args, **kwargs):
        with original_temp(*args, **kwargs) as stream:

            class InterruptedStream:
                name = stream.name

                def write(self, content):
                    stream.write(content[:2])
                    if signal_during_write:
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        raise OSError("injected write failure")

            yield InterruptedStream()

    monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", staging)
    destination = tmp_path / "out"
    assert cli.main(["transcribe", "model", str(source), "--output", str(destination)]) == (
        143 if signal_during_write else 1
    )
    assert not destination.exists()
    assert not list(tmp_path.glob(".phonon-*"))
    assert capfd.readouterr().out == ""


def test_signal_during_option_parsing(source, managed_upstream, monkeypatch):
    original_parse = cli.parse_duration

    def parse(value):
        os.kill(os.getpid(), signal.SIGTERM)
        return original_parse(value)

    monkeypatch.setattr(cli, "parse_duration", parse)
    assert cli.main(["transcribe", "model", str(source), "--segment-duration=30"]) == 143
    assert not managed_upstream.exists()
