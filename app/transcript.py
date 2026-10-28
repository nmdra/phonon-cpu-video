"""Bounded, model-independent transcript presentation.

Timing is limited to signed platform-integer milliseconds. Subtitle conversion
rounds decimal seconds half up exactly once, rather than using binary round().
"""

import json
import math
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TRANSCRIPT_LIMIT = 4 * 1024 * 1024
CONTENT_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json",
}


class TranscriptError(Exception):
    """Invalid options, malformed transcript, or bounded serialization failure."""


@dataclass(frozen=True)
class OutputOptions:
    format: str = "txt"
    segment_duration: float | None = None


@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    text: str


def ensure_limit(content: bytes) -> bytes:
    """Return content unchanged, or reject transcript bytes over the shared limit."""
    if len(content) > TRANSCRIPT_LIMIT:
        raise TranscriptError("Transcript exceeds the byte limit")
    return content


def _seconds(value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptError("Invalid time value")
    try:
        seconds = float(value)
        if (
            not math.isfinite(seconds)
            or seconds < 0
            or (positive and seconds == 0)
            or Decimal(str(seconds)) * 1000 > sys.maxsize
        ):
            raise TranscriptError("Invalid time value")
        return seconds
    except (OverflowError, ValueError) as exc:
        raise TranscriptError("Invalid time value") from exc


def parse_duration(value: str) -> float:
    """Parse a finite positive target, bounded by representable milliseconds."""
    try:
        return _seconds(float(value), positive=True)
    except (OverflowError, ValueError, TypeError) as exc:
        raise TranscriptError("Invalid segment duration") from exc


def _reject_constant(value: str) -> None:
    raise TranscriptError("Invalid JSON number")


def parse_transcript(content: bytes) -> dict:
    """Validate native transcript JSON without rewriting its captured bytes."""
    try:
        data = json.loads(ensure_limit(content).decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise TranscriptError("Invalid transcript JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise TranscriptError("Transcript requires string text")
    return data


def _cues(data: dict) -> list[_Cue]:
    segments = data.get("segments", [])
    if not isinstance(segments, list):
        raise TranscriptError("Transcript requires a segment list")
    cues = []
    previous_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise TranscriptError("Invalid transcript segment")
        start = _seconds(segment.get("start"))
        end = _seconds(segment.get("end"))
        if start >= end or start < previous_end:
            raise TranscriptError("Invalid segment timing or order")
        previous_end = end
        text = segment["text"].strip()
        if text:
            cues.append(_Cue(start, end, text))
    if not cues and data["text"].strip():
        raise TranscriptError("Nonempty transcript has no timed text")
    return cues


def _group(cues: list[_Cue], target: float) -> list[_Cue]:
    groups: list[_Cue] = []
    current: list[_Cue] = []

    def emit() -> None:
        groups.append(
            _Cue(current[0].start, current[-1].end, " ".join(cue.text for cue in current))
        )
        current.clear()

    for cue in cues:
        if current and cue.start - current[-1].end >= target:
            emit()
        current.append(cue)
        if current[-1].end - current[0].start >= target:
            emit()
    if current:
        emit()
    return groups


def _milliseconds(seconds: float) -> int:
    return int((Decimal(str(seconds)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _timestamp(milliseconds: int, separator: str) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _subtitles(cues: list[_Cue], format: str) -> str:
    blocks = ["WEBVTT\n\n"] if format == "vtt" else []
    separator = "." if format == "vtt" else ","
    for index, cue in enumerate(cues, 1):
        start, end = _milliseconds(cue.start), _milliseconds(cue.end)
        if end <= start:
            raise TranscriptError("Cue collapses at millisecond precision")
        payload = " ".join(cue.text.replace("\0", "\ufffd").split())
        payload = payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        prefix = f"{index}\n" if format == "srt" else ""
        blocks.append(
            f"{prefix}{_timestamp(start, separator)} --> {_timestamp(end, separator)}\n"
            f"{payload}\n\n"
        )
    return "".join(blocks)


def render_transcript(
    content: bytes, options: OutputOptions, *, json_style: str = "native"
) -> bytes:
    """Render upstream CLI/verbose HTTP JSON without modifying native metadata.

    ``json_style='text'`` limits ordinary HTTP JSON to text and, when requested,
    output groups. Native JSON keeps source segments and all other fields.
    """
    if options.format not in CONTENT_TYPES or json_style not in {"native", "text"}:
        raise TranscriptError("Invalid output format")
    target = options.segment_duration
    if target is not None:
        target = _seconds(target, positive=True)
    data = parse_transcript(content)
    cues = []
    if target is not None or options.format in {"srt", "vtt"}:
        cues = _cues(data)
        if target is not None:
            cues = _group(cues, target)
    try:
        if options.format == "json":
            result = data.copy() if json_style == "native" else {"text": data["text"]}
            if target is not None:
                result["output_segments"] = [
                    {"id": i, "start": cue.start, "end": cue.end, "text": cue.text}
                    for i, cue in enumerate(cues)
                ]
                result["output_segment_count"] = len(cues)
            text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        elif options.format == "txt":
            text = "\n\n".join(cue.text for cue in cues) if target is not None else data["text"]
            text = text.rstrip("\n") + "\n" if text else ""
        else:
            text = _subtitles(cues, options.format)
        return ensure_limit(text.encode("utf-8"))
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise TranscriptError("Cannot serialize transcript") from exc
