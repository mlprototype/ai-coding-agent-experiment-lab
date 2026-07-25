"""Experiment specification loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentlab.models import ExperimentSpec


class SpecLoadError(ValueError):
    """Raised when a specification cannot be parsed or validated."""


def load_experiment_spec(path: Path) -> ExperimentSpec:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SpecLoadError(f"could not read YAML: {error}") from error

    if not isinstance(raw, dict):
        raise SpecLoadError("experiment specification must be a YAML mapping")

    try:
        return ExperimentSpec.model_validate(raw)
    except ValidationError as error:
        raise SpecLoadError(str(error)) from error

