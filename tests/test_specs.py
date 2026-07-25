from __future__ import annotations

from pathlib import Path

import pytest

from agentlab.specs import SpecLoadError, load_experiment_spec


def test_example_yaml_loads() -> None:
    path = Path("experiments/examples/workflow-smoke.yaml")

    spec = load_experiment_spec(path)

    assert spec.experiment_id == "workflow-smoke"


@pytest.mark.parametrize("content", ["- item\n- item\n", "plain text\n", "", "null\n"])
def test_rejects_yaml_that_is_not_a_mapping(tmp_path: Path, content: str) -> None:
    path = tmp_path / "not-a-mapping.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SpecLoadError, match="must be a YAML mapping"):
        load_experiment_spec(path)


def test_reports_yaml_syntax_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("key: [unterminated\n", encoding="utf-8")

    with pytest.raises(SpecLoadError, match="could not read YAML") as error:
        load_experiment_spec(path)

    assert "expected" in str(error.value)


def test_reports_file_read_oserror(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(SpecLoadError, match="could not read YAML") as error:
        load_experiment_spec(path)

    assert "No such file" in str(error.value)


def test_reports_utf8_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"\xff")

    with pytest.raises(SpecLoadError, match="could not read YAML") as error:
        load_experiment_spec(path)

    assert "utf-8" in str(error.value)
