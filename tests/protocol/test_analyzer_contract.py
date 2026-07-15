from __future__ import annotations

from typing import Any

import pytest

from skill_temple.browser_models import RemoveJsonPathMutation
from skill_temple.protocol_evidence import analyze_replay_response


@pytest.mark.parametrize(
    ("status", "response_value", "expected"),
    [
        (None, None, "unknown_response"),
        (401, {"error": "authentication required"}, "authentication_failure"),
        (409, {"error": "state conflict"}, "conflict"),
        (429, {"error": "rate limited"}, "rate_limited"),
        (500, {"error": "server failure"}, "server_failure"),
        (304, None, "redirect_or_cache_response"),
    ],
)
def test_analyzer_returns_factual_classification_without_execution_decision(
    status: int | None,
    response_value: Any,
    expected: str,
) -> None:
    result = analyze_replay_response(
        status=status,
        content_type="application/json",
        response_value=response_value,
        mutation=None,
        source_content_type="application/json",
    )

    assert result["classification"] == expected
    assert result["observations"]["http_status"] == status
    assert "execution" not in result
    assert "quality" not in result
    assert "valid" not in result
    assert "experiment_status" not in result


@pytest.mark.parametrize(
    (
        "response_value",
        "expected_classification",
        "expected_strength",
        "expected_semantic",
    ),
    [
        ({"message": "identifier is invalid"}, "unknown_rejection", "none", "none"),
        (
            {"field": "/resource/id", "code": "field_required"},
            "validation_rejection",
            "strong_structured",
            "field_required",
        ),
        (
            {"field": "/other/id", "code": "field_required"},
            "unknown_rejection",
            "weak_text_match",
            "field_reference",
        ),
        (
            {"field": "/resource/id", "code": "unknown_custom_code"},
            "unknown_rejection",
            "strong_structured",
            "field_reference",
        ),
    ],
)
def test_analyzer_requires_exact_structured_target_evidence(
    response_value: Any,
    expected_classification: str,
    expected_strength: str,
    expected_semantic: str,
) -> None:
    result = analyze_replay_response(
        status=422,
        content_type="application/json",
        response_value=response_value,
        mutation=RemoveJsonPathMutation(
            type="remove_json_path",
            path="/resource/id",
        ),
    )

    assert result["classification"] == expected_classification
    assert result["validation_evidence"]["strength"] == expected_strength
    assert result["validation_evidence"]["semantic"] == expected_semantic


def test_unknown_evidence_is_a_normal_empty_hint_result() -> None:
    result = analyze_replay_response(
        status=422,
        content_type="text/plain",
        response_value="possibly invalid but not structured evidence",
        mutation=RemoveJsonPathMutation(type="remove_json_path", path="/resource/id"),
    )

    assert result["classification"] == "unknown_rejection"
    assert result["hints"] == []
    assert result["validation_evidence"]["semantic"] == "none"
