"""CLI dispatch with transparent native delegation and supervised transcription."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from app.conversion import MediaError, prepared_audio
from app.settings import Settings

UPSTREAM_ENTRYPOINT = "/usr/local/bin/phonon-entrypoint"
_SIGNAL_GRACE_SECONDS = 5.0


class _Termination(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def _transcribe(arguments: Sequence[str], settings: Settings) -> int:
    model, source, *options = arguments
    child: subprocess.Popen[bytes] | None = None
    deadline: float | None = None
    received_signal: int | None = None

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
    try:
        with prepared_audio(Path(source), settings) as audio:
            child = subprocess.Popen(
                [UPSTREAM_ENTRYPOINT, "transcribe", model, str(audio), *options],
                start_new_session=True,
            )
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
        return 128 + exc.signum
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
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
                parser.parse_args(arguments[1:])
            return _transcribe(arguments[1:], Settings.from_env())
        os.execv(UPSTREAM_ENTRYPOINT, [UPSTREAM_ENTRYPOINT, *arguments])
    except MediaError as exc:
        print(f"Media error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"phonon-video: {exc}", file=sys.stderr)
        return 1
    return 0
