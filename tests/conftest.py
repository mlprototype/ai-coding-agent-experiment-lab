from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture
def valid_spec_data() -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "experiment_id": "workflow-smoke",
            "title": "Workflow smoke test",
            "research_question": "Does workflow affect the gate result?",
            "hypothesis": "Staged execution will improve the pass rate.",
            "comparison_axis": "workflow",
            "workflow": "one_shot",
            "provider": "replay",
            "control": "one_shot",
            "treatments": ["staged"],
            "fixed_factors": {"fixture_revision": "v1"},
            "task_ids": ["task-1"],
            "repetitions": 2,
            "random_seed": 42,
            "quality_gate": {"acceptance": [["pytest", "-q"]]},
            "stop_conditions": {"max_failures": 2},
            "execution_mode": "replay",
            "replay": {"recording_path": "recordings/test.jsonl"},
            "live": None,
        }

    return build

