"""CLI dispatch with transparent native delegation and supervised transcription."""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from app.conversion import MediaError, prepared_audio
from app.settings import Settings
from app.transcript import (
    TRANSCRIPT_LIMIT,
    OutputOptions,
    TranscriptError,
    ensure_limit,
    parse_duration,
    parse_transcript,
    render_transcript,
)

UPSTREAM_ENTRYPOINT = "/usr/local/bin/phonon-entrypoint"
_SIGNAL_GRACE_SECONDS = 5.0


class _Termination(Exception):
    def __init__(self, signum: int):
        self.signum = signum


class _UsageError(Exception):
    pass


def _export_options(options: Sequence[str]) -> tuple[list[str], OutputOptions | None, str | None]:
    values: dict[str, str] = {}
    forwarded: list[str] = []
    index = 0
    owned = {"--output-format", "--segment-duration", "--output"}
    while index < len(options):
        argument = options[index]
        name, separator, value = argument.partition("=")
        if name not in owned:
            forwarded.append(argument)
            index += 1
            continue
        if name in values:
            raise _UsageError(f"duplicate {name}")
        if not separator:
            index += 1
            if index == len(options) or options[index].startswith("--"):
                raise _UsageError(f"missing value for {name}")
            value = options[index]
        if not value:
            raise _UsageError(f"missing value for {name}")
        values[name] = value
        index += 1
    format_name = values.get("--output-format")
    if format_name is not None and format_name not in {"txt", "srt", "vtt", "json"}:
        raise _UsageError("--output-format must be txt, srt, vtt, or json")
    if "--json" in forwarded and format_name not in {None, "json"}:
        raise _UsageError("--json conflicts with --output-format")
    duration = None
    if "--segment-duration" in values:
        try:
            duration = parse_duration(values["--segment-duration"])
        except TranscriptError as exc:
            raise _UsageError(str(exc)) from exc
    rendering = None
    if format_name is not None or duration is not None:
        rendering = OutputOptions(
            format_name or ("json" if "--json" in forwarded else "txt"), duration
        )
        if "--json" not in forwarded:
            forwarded.append("--json")
    return forwarded, rendering, values.get("--output")


def _destination(path: str) -> Path:
    destination = Path(path)
    if os.path.lexists(destination):
        raise OSError("output destination already exists")
    if not destination.parent.is_dir():
        raise OSError("output parent directory does not exist")
    if not os.access(destination.parent, os.W_OK | os.X_OK):
        raise OSError("output parent directory is not writable")
    return destination


def _capture(
    child: subprocess.Popen[bytes], check_deadline: Callable[[], None]
) -> tuple[int, bytes]:
    assert child.stdout is not None
    content = bytearray()
    os.set_blocking(child.stdout.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        pipe_open = True
        while True:
            check_deadline()
            events = selector.select(timeout=0.1)
            # Once the direct child exits, drain only bytes already in the pipe.
            # A descendant can keep the descriptor open indefinitely.
            exited = child.poll() is not None
            if pipe_open and (events or exited):
                while True:
                    try:
                        chunk = os.read(child.stdout.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        selector.unregister(child.stdout)
                        pipe_open = False
                        break
                    if len(content) + len(chunk) > TRANSCRIPT_LIMIT:
                        raise TranscriptError("transcript exceeds byte limit")
                    content.extend(chunk)
            if exited:
                return child.wait(), bytes(content)


def _publish(destination: Path, content: bytes, check_signal: Callable[[], None]) -> None:
    staging: Path | None = None
    published = False
    try:
        check_signal()
        with tempfile.NamedTemporaryFile(
            prefix=".phonon-", dir=destination.parent, delete=False
        ) as stream:
            staging = Path(stream.name)
            check_signal()
            stream.write(content)
        check_signal()
        blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            check_signal()
            os.link(staging, destination)
            published = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    finally:
        if staging is not None:
            try:
                staging.unlink()
            except OSError as exc:
                if published:
                    print(f"phonon-video: staging cleanup warning: {exc}", file=sys.stderr)
                else:
                    raise
    try:
        check_signal()
    except _Termination:
        print("phonon-video: complete output file was published", file=sys.stderr)
        raise


def _transcribe(arguments: Sequence[str], settings: Settings) -> int:
    model, source, *options = arguments
    child: subprocess.Popen[bytes] | None = None
    deadline: float | None = None
    received_signal: int | None = None
    published = False

    def forward(signum: int, _frame: object) -> None:
        nonlocal deadline, received_signal
        received_signal = signum
        if child is None:
            raise _Termination(signum)
        deadline = time.monotonic() + _SIGNAL_GRACE_SECONDS
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    previous = {
        sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }

    def check_signal() -> None:
        if received_signal is not None:
            raise _Termination(received_signal)

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            assert child is not None
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise _Termination(received_signal or signal.SIGTERM)

    try:
        options, rendering, output = _export_options(options)
        managed = rendering is not None or output is not None
        destination = _destination(output) if output is not None and output != "-" else None
        check_signal()
        with prepared_audio(Path(source), settings) as audio:
            child = subprocess.Popen(
                [UPSTREAM_ENTRYPOINT, "transcribe", model, str(audio), *options],
                start_new_session=True,
                stdout=subprocess.PIPE if managed else None,
            )
            if managed:
                code, content = _capture(child, check_deadline)
                if code:
                    return code if code >= 0 else 128 - code
                check_signal()
                if rendering is not None:
                    content = render_transcript(content, rendering)
                else:
                    if "--json" in options:
                        parse_transcript(content)
                    try:
                        content.decode("utf-8")
                    except UnicodeError as exc:
                        raise TranscriptError("transcript is not UTF-8") from exc
                content = ensure_limit(content)
                check_signal()
                if destination is not None:
                    _publish(destination, content, check_signal)
                    published = True
                else:
                    check_signal()
                    sys.stdout.buffer.write(content)
                    sys.stdout.buffer.flush()
                check_signal()
                return 0
            while True:
                try:
                    code = child.wait(timeout=0.2)
                    return code if code >= 0 else 128 - code
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
                        return 128 + (received_signal or signal.SIGTERM)
    except _Termination as exc:
        if published:
            print("phonon-video: complete output file was published", file=sys.stderr)
        return 128 + exc.signum
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            if child.stdout is not None:
                child.stdout.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _serve(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="phonon serve-video")
    parser.add_argument("model")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-key", default=os.environ.get("PHONON_VIDEO_API_KEY"))
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be positive")
    from app.api import run_server

    run_server(
        model=args.model,
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        threads=args.threads,
        settings=Settings.from_env(),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "serve-video":
            return _serve(arguments[1:])
        if arguments and arguments[0] == "transcribe":
            if len(arguments) < 3 or arguments[1] in ("-h", "--help"):
                parser = argparse.ArgumentParser(
                    prog="phonon transcribe",
                    usage="%(prog)s MODEL FILE [upstream options]",
                    description="Normalize local audio/video before upstream transcription.",
                )
                parser.add_argument("model")
                parser.add_argument("file")
                parser.add_argument("--output-format", choices=("txt", "srt", "vtt", "json"))
                parser.add_argument("--segment-duration", metavar="SECONDS")
                parser.add_argument("--output", metavar="PATH", help="new file, or - for stdout")
                parser.parse_args(arguments[1:])
            return _transcribe(arguments[1:], Settings.from_env())
        os.execv(UPSTREAM_ENTRYPOINT, [UPSTREAM_ENTRYPOINT, *arguments])
    except _UsageError as exc:
        print(f"phonon-video: {exc}", file=sys.stderr)
        return 2
    except TranscriptError as exc:
        print(f"phonon-video: transcript output error: {exc}", file=sys.stderr)
        return 1
    except MediaError as exc:
        print(f"Media error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"phonon-video: {exc}", file=sys.stderr)
        return 1
    return 0
