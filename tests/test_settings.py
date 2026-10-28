from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.settings import Settings


def test_defaults():
    settings = Settings()
    assert settings.max_upload_bytes == 100 * 1024**2
    assert settings.max_duration == 600
    assert settings.max_concurrency == 1
    assert settings.scratch_dir == Path("/tmp/phonon-video")
    assert settings.scratch_budget_bytes == 256 * 1024**2
    assert settings.required_scratch_bytes == settings.max_concurrency * (
        2 * settings.max_upload_bytes + settings.max_output_bytes
    )
    assert settings.scratch_budget_bytes >= settings.required_scratch_bytes
    with pytest.raises(FrozenInstanceError):
        settings.max_duration = 2  # pyright: ignore[reportAttributeAccessIssue]


def test_environment(monkeypatch):
    monkeypatch.setenv("PHONON_VIDEO_MAX_DURATION", "10.5")
    monkeypatch.setenv("PHONON_VIDEO_SCRATCH_DIR", "/tmp/example")
    monkeypatch.setenv("PHONON_VIDEO_MAX_UPLOAD_BYTES", "1024")
    settings = Settings.from_env()
    assert settings.max_duration == 10.5
    assert settings.max_upload_bytes == 1024
    assert settings.scratch_dir == Path("/tmp/example")


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_duration", float("nan")),
        ("probe_timeout", float("inf")),
        ("conversion_timeout", 0),
        ("inference_timeout", -1),
        ("upload_timeout", 0),
        ("max_upload_bytes", -1),
        ("max_concurrency", 1.5),
        ("max_concurrency", True),
        ("scratch_budget_bytes", 100),
        ("max_text_fields", 0),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValueError):
        replace(Settings(), **{field: value})


def test_concurrency_requires_reserved_scratch():
    with pytest.raises(ValueError, match="concurrency"):
        Settings(max_concurrency=2)
    assert Settings(max_concurrency=2, scratch_budget_bytes=512 * 1024**2)


def test_invalid_environment(monkeypatch):
    monkeypatch.setenv("PHONON_VIDEO_MAX_CONCURRENCY", "1.5")
    with pytest.raises(ValueError, match="PHONON_VIDEO_MAX_CONCURRENCY"):
        Settings.from_env()
