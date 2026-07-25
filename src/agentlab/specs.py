"""Experiment specification loading."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentlab.models import ExperimentSpec


class SpecLoadError(ValueError):
    """Raised when a specification cannot be parsed or validated."""


@dataclass(frozen=True)
class LoadedExperimentSpec:
    """A validated Spec and the digest of the exact bytes used to build it."""

    spec: ExperimentSpec
    sha256: str


def load_experiment_spec_document(path: Path) -> LoadedExperimentSpec:
    """Read a Spec once, then parse, validate, and hash those same bytes."""
    try:
        source_bytes = path.read_bytes()
        raw: Any = yaml.safe_load(source_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SpecLoadError(f"could not read YAML: {error}") from error

    if not isinstance(raw, dict):
        raise SpecLoadError("experiment specification must be a YAML mapping")

    try:
        spec = ExperimentSpec.model_validate(raw)
    except ValidationError as error:
        raise SpecLoadError(str(error)) from error
    return LoadedExperimentSpec(
        spec=spec,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def load_experiment_spec(path: Path) -> ExperimentSpec:
    return load_experiment_spec_document(path).spec
