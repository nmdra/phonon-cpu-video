"""Pure transcript tests. No inference, network, or model fixtures."""

import copy
import json
import sys
from dataclasses import FrozenInstanceError

import pytest

from app.transcript import (
    CONTENT_TYPES,
    TRANSCRIPT_LIMIT,
    OutputOptions,
    TranscriptError,
    ensure_limit,
    parse_duration,
    render_transcript,
)


@pytest.fixture
def cli_transcript():
    # Observed fermion-research 0.2.15, fermion/transcribe.py:308-328.
    # CLI words use `text`; native decoder metadata must survive grouping.
    return {
        "text": "  Café 世界. Next.  ",
        "model": "fixture/model",
        "profile": "cpu",
        "backend": "fixture",
        "engine": "fixture",
        "duration_seconds": 32.0,
        "decode_seconds": 0.2,
        "wall_seconds": 0.3,
        "segment_count": 2,
        "segments": [
            {"id": 7, "start": 0.0, "end": 12.0, "text": " Café 世界. "},
            {"id": 8, "start": 12.0, "end": 32.0, "text": " Next. "},
        ],
        "words": [{"text": "Café", "start": 0.0, "end": 1.0}],
        "truncated": False,
    }


@pytest.fixture
def http_transcript(cli_transcript):
    # Observed fermion/server.py:974-982,1025-1074 and _speech/cues.py:194-208
    # in fermion-research 0.2.15. HTTP words use `word`, not CLI's `text`.
    return {
        "task": "transcribe",
        "language": "en",
        "duration": 32.0,
        "text": cli_transcript["text"],
        "segments": [
            {**segment, "seek": 0, "tokens": [], "avg_logprob": -0.5}
            for segment in cli_transcript["segments"]
        ],
        "words": [{"word": "Café", "start": 0.0, "end": 1.0}],
        "x_fermion": {"model": "fixture/model", "segment_count": 2},
    }


def encoded(data):
    return json.dumps(data, ensure_ascii=False).encode()


def timed(segments, text="transcript"):
    return encoded({"text": text, "segments": segments})


def segment(start, end, text: object = "word"):
    return {"start": start, "end": end, "text": text}


def test_public_options_and_mime_types():
    assert OutputOptions() == OutputOptions("txt", None)
    with pytest.raises(FrozenInstanceError):
        OutputOptions().format = "srt"
    assert CONTENT_TYPES == {
        "txt": "text/plain; charset=utf-8",
        "srt": "application/x-subrip; charset=utf-8",
        "vtt": "text/vtt; charset=utf-8",
        "json": "application/json",
    }


def test_plain_txt_uses_top_text_not_segments(cli_transcript):
    assert render_transcript(encoded(cli_transcript), OutputOptions()) == (
        "  Café 世界. Next.  \n".encode()
    )
    assert render_transcript(b'{"text":"first\\nsecond\\n\\n"}', OutputOptions()) == (
        b"first\nsecond\n"
    )
    assert render_transcript(b'{"text":"only","segments":null}', OutputOptions()) == b"only\n"


@pytest.mark.parametrize("format", ["txt", "srt", "vtt"])
def test_cli_and_http_equivalent_formats(cli_transcript, http_transcript, format):
    options = OutputOptions(format, 30)
    assert render_transcript(encoded(cli_transcript), options) == render_transcript(
        encoded(http_transcript), options
    )


def test_exact_source_subtitles(cli_transcript):
    assert (
        render_transcript(encoded(cli_transcript), OutputOptions("srt"))
        == (
            "1\n00:00:00,000 --> 00:00:12,000\nCafé 世界.\n\n"
            "2\n00:00:12,000 --> 00:00:32,000\nNext.\n\n"
        ).encode()
    )
    assert (
        render_transcript(encoded(cli_transcript), OutputOptions("vtt"))
        == (
            "WEBVTT\n\n00:00:00.000 --> 00:00:12.000\nCafé 世界.\n\n"
            "00:00:12.000 --> 00:00:32.000\nNext.\n\n"
        ).encode()
    )


def test_exact_grouped_subtitles_and_txt(cli_transcript):
    content = encoded(cli_transcript)
    assert (
        render_transcript(content, OutputOptions("srt", 30))
        == ("1\n00:00:00,000 --> 00:00:32,000\nCafé 世界. Next.\n\n").encode()
    )
    assert (
        render_transcript(content, OutputOptions("vtt", 30))
        == ("WEBVTT\n\n00:00:00.000 --> 00:00:32.000\nCafé 世界. Next.\n\n").encode()
    )
    assert render_transcript(content, OutputOptions("txt", 30)) == "Café 世界. Next.\n".encode()


@pytest.mark.parametrize("fixture", ["cli_transcript", "http_transcript"])
def test_grouped_native_json_preserves_all_metadata(request, fixture):
    data = request.getfixturevalue(fixture)
    original = copy.deepcopy(data)
    content = encoded(data)
    result = json.loads(render_transcript(content, OutputOptions("json", 30)))
    assert result.pop("output_segments") == [
        {"id": 0, "start": 0.0, "end": 32.0, "text": "Café 世界. Next."}
    ]
    assert result.pop("output_segment_count") == 1
    assert result == original
    assert data == original
    assert content == encoded(original)
    assert json.loads(render_transcript(content, OutputOptions("json"))) == original


def test_ordinary_http_grouped_json_exact_bytes(http_transcript):
    assert (
        render_transcript(encoded(http_transcript), OutputOptions("json", 30), json_style="text")
        == (
            '{"text": "  Café 世界. Next.  ", "output_segments": '
            '[{"id": 0, "start": 0.0, "end": 32.0, "text": "Café 世界. Next."}], '
            '"output_segment_count": 1}'
        ).encode()
    )


def test_grouping_threshold_silence_coarse_and_final_group():
    content = timed(
        [
            segment(0, 10, " a "),
            segment(10, 30, " b "),  # Exact threshold emits immediately.
            segment(31, 35, "c"),
            segment(65, 70, "d"),  # Exact target gap flushes c before d.
            segment(71, 110, "e"),  # Never split a coarse source segment.
            segment(111, 112, "f"),  # Emit the short final group.
        ]
    )
    result = json.loads(render_transcript(content, OutputOptions("json", 30)))
    assert result["output_segments"] == [
        {"id": 0, "start": 0, "end": 30, "text": "a b"},
        {"id": 1, "start": 31, "end": 35, "text": "c"},
        {"id": 2, "start": 65, "end": 110, "text": "d e"},
        {"id": 3, "start": 111, "end": 112, "text": "f"},
    ]
    assert render_transcript(content, OutputOptions("txt", 30)) == b"a b\n\nc\n\nd e\n\nf\n"


def test_grouping_does_not_split_coarse_segments_for_tiny_target():
    content = timed([segment(0, 100, "a"), segment(100, 200, "b")])
    result = json.loads(render_transcript(content, OutputOptions("json", 5e-324)))
    assert result["output_segments"] == [
        {"id": 0, "start": 0, "end": 100, "text": "a"},
        {"id": 1, "start": 100, "end": 200, "text": "b"},
    ]


@pytest.mark.parametrize("segments", [None, []])
@pytest.mark.parametrize("format,expected", [("txt", b""), ("srt", b""), ("vtt", b"WEBVTT\n\n")])
def test_empty_transcript(format, expected, segments):
    data = {"text": ""}
    if segments is not None:
        data["segments"] = segments
    assert render_transcript(encoded(data), OutputOptions(format)) == expected
    assert render_transcript(encoded(data), OutputOptions(format, 30)) == expected
    assert json.loads(render_transcript(encoded(data), OutputOptions("json", 30))) == {
        **data,
        "output_segments": [],
        "output_segment_count": 0,
    }


def test_blank_segments_are_removed_but_do_not_bridge_long_silence():
    content = timed([segment(0, 1, "a"), segment(1, 60, " \n "), segment(60, 61, "b")])
    assert render_transcript(content, OutputOptions("txt", 30)) == b"a\n\nb\n"
    assert render_transcript(timed([segment(0, 1, " \n ")], ""), OutputOptions("srt")) == b""


@pytest.mark.parametrize("text", ["a", " \na \n"])
@pytest.mark.parametrize("segments", [[], [segment(0, 1, " ")]])
@pytest.mark.parametrize(
    "options", [OutputOptions("srt"), OutputOptions("vtt"), OutputOptions("txt", 1)]
)
def test_nonempty_without_timed_text_fails(text, segments, options):
    with pytest.raises(TranscriptError):
        render_transcript(timed(segments, text), options)


def test_subtitle_payload_is_literal_single_line():
    content = timed([segment(0, 1, " <b>A & B</b>\n\n00:00 --> next\t\0 世界 ")])
    assert (
        render_transcript(content, OutputOptions("srt"))
        == (
            "1\n00:00:00,000 --> 00:00:01,000\n"
            "&lt;b&gt;A &amp; B&lt;/b&gt; 00:00 --&gt; next � 世界\n\n"
        ).encode()
    )
    assert (
        render_transcript(content, OutputOptions("txt", 1))
        == ("<b>A & B</b>\n\n00:00 --> next\t\0 世界\n").encode()
    )


def test_half_up_rounding_with_minute_and_hour_carries():
    content = timed([segment(59.9995, 3599.9995, "carry"), segment(3600, 3600.0005, "half")])
    assert render_transcript(content, OutputOptions("srt")) == (
        b"1\n00:01:00,000 --> 01:00:00,000\ncarry\n\n2\n01:00:00,000 --> 01:00:00,001\nhalf\n\n"
    )
    assert render_transcript(content, OutputOptions("vtt")) == (
        b"WEBVTT\n\n00:01:00.000 --> 01:00:00.000\ncarry\n\n01:00:00.000 --> 01:00:00.001\nhalf\n\n"
    )


@pytest.mark.parametrize("format", ["srt", "vtt"])
def test_submillisecond_collapse_is_error(format):
    with pytest.raises(TranscriptError):
        render_transcript(timed([segment(0.0001, 0.0004)]), OutputOptions(format))


@pytest.mark.parametrize(
    "value",
    [True, False, None, "1", -1, float("nan"), float("inf"), -float("inf"), 1e308, 10**1000],
)
@pytest.mark.parametrize("field", ["start", "end"])
def test_invalid_timestamp_types_and_ranges(value, field):
    cue = segment(0, 1)
    cue[field] = value
    with pytest.raises(TranscriptError):
        render_transcript(timed([cue]), OutputOptions("srt"))


@pytest.mark.parametrize(
    "segments",
    [
        [segment(1, 1)],
        [segment(2, 1)],
        [segment(2, 3), segment(0, 1)],
        [segment(0, 2), segment(1, 3)],
        [segment(0, 1), segment(0.5, 2, " ")],
        [{"start": 0, "text": "x"}],
        [segment(0, 1, 5)],
        [None],
        {},
        None,
    ],
)
def test_invalid_segments(segments):
    with pytest.raises(TranscriptError):
        render_transcript(timed(segments), OutputOptions("txt", 30))


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"not JSON",
        b"[]",
        b"null",
        b"{}",
        b'{"text":null}',
        b'{"text":false}',
        b'{"text":12}',
        b'{"text":[]}',
        b'{"text":{}}',
        b'{"text":"\\ud800"}',
        b'{"text":"x","metadata":1e400}',
    ],
)
def test_parse_and_serialization_failures_are_normalized(content):
    with pytest.raises(TranscriptError):
        render_transcript(content, OutputOptions("json"))


def test_deeply_nested_json_is_normalized():
    # Python 3.13's JSON C recursion guard is separate from Python's limit.
    depth = max(10000, sys.getrecursionlimit() + 100)
    content = b'{"text":"x","metadata":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    with pytest.raises(TranscriptError):
        render_transcript(content, OutputOptions("json"))


@pytest.mark.parametrize("format", ["txt", "json", "srt", "vtt"])
def test_every_format_requires_top_level_string_text(format):
    with pytest.raises(TranscriptError):
        render_transcript(b'{"segments":[]}', OutputOptions(format))


@pytest.mark.parametrize(
    "value",
    ["", "words", "0", "-1", "nan", "inf", "-inf", "1e308", "1e9999", "1e-9999", "9" * 10000],
)
def test_parse_duration_rejects_invalid_values(value):
    with pytest.raises(TranscriptError):
        parse_duration(value)


@pytest.mark.parametrize(
    "value,expected", [("30", 30.0), (" 0.25 ", 0.25), ("1e2", 100), ("5e-324", 5e-324)]
)
def test_parse_duration_accepts_positive_finite_seconds(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 1e308, "30"])
def test_direct_output_options_cannot_bypass_duration_validation(value):
    with pytest.raises(TranscriptError):
        render_transcript(b'{"text":""}', OutputOptions("txt", value))


def test_invalid_format_and_json_style():
    with pytest.raises(TranscriptError):
        render_transcript(b'{"text":""}', OutputOptions("unknown"))
    with pytest.raises(TranscriptError):
        render_transcript(b'{"text":""}', OutputOptions("json"), json_style="unknown")


def test_shared_limit_and_input_boundary():
    assert TRANSCRIPT_LIMIT == 4 * 1024 * 1024
    content = b"x" * TRANSCRIPT_LIMIT
    assert ensure_limit(content) is content
    with pytest.raises(TranscriptError):
        ensure_limit(content + b"x")
    with pytest.raises(TranscriptError):
        render_transcript(b" " * (TRANSCRIPT_LIMIT + 1), OutputOptions())
    prefix, suffix = b'{"text":"', b'"}'
    data = prefix + b"x" * (TRANSCRIPT_LIMIT - len(prefix) - len(suffix)) + suffix
    assert len(data) == TRANSCRIPT_LIMIT
    assert len(render_transcript(data, OutputOptions())) == TRANSCRIPT_LIMIT - 10


def test_subtitle_output_expansion_over_limit():
    content = timed([segment(0, 1, "&" * (TRANSCRIPT_LIMIT // 4))])
    assert len(content) < TRANSCRIPT_LIMIT
    with pytest.raises(TranscriptError):
        render_transcript(content, OutputOptions("vtt"))


def test_json_serialization_expansion_over_limit():
    content = timed([segment(0, 1, "x" * (TRANSCRIPT_LIMIT // 2))])
    assert len(content) < TRANSCRIPT_LIMIT
    with pytest.raises(TranscriptError):
        render_transcript(content, OutputOptions("json", 30))
