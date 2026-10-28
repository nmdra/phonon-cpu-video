"""Validated resource limits shared by local conversion and HTTP ingress."""

import math
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    max_upload_bytes: int = 100 * 1024**2
    max_duration: float = 600.0
    probe_timeout: float = 30.0
    conversion_timeout: float = 120.0
    inference_timeout: float = 900.0
    upload_timeout: float = 120.0
    max_concurrency: int = 1
    scratch_budget_bytes: int = 256 * 1024**2
    scratch_dir: Path = Path("/tmp/phonon-video")
    max_text_fields: int = 16
    max_text_field_bytes: int = 4096

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "scratch_dir":
                object.__setattr__(self, field.name, Path(value))
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field.name} must be finite and positive")
            elif field.name in {
                "max_upload_bytes",
                "max_concurrency",
                "scratch_budget_bytes",
                "max_text_fields",
                "max_text_field_bytes",
            } and not isinstance(value, int):
                raise ValueError(f"{field.name} must be an integer")
        if self.scratch_budget_bytes < self.required_scratch_bytes:
            raise ValueError("scratch_budget_bytes cannot support configured concurrency")

    @property
    def max_output_bytes(self) -> int:
        """PCM payload plus bounded WAV header allowance."""
        return math.ceil(self.max_duration * 16000 * 2) + 65536

    @property
    def required_scratch_bytes(self) -> int:
        """Reserve uploaded input, immutable conversion copy, and WAV per request."""
        return self.max_concurrency * (2 * self.max_upload_bytes + self.max_output_bytes)

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        values = {}
        for field in fields(cls):
            raw = os.environ.get(f"PHONON_VIDEO_{field.name.upper()}")
            if raw is not None:
                try:
                    values[field.name] = type(getattr(defaults, field.name))(raw)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Invalid PHONON_VIDEO_{field.name.upper()}") from exc
        return cls(**values)
