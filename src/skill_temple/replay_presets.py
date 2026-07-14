"""Client-side presets for the generic replay request payload.

These helpers preserve familiar workflow names without adding mode-specific types or
branching to the core browser API. Every helper returns the same ReplayRequestPayload.
"""

from __future__ import annotations

from typing import Any

from .browser_models import ReplayRequestPayload


def _payload(
    *,
    session_id: str,
    objective: str,
    source_experiment_id: str,
    source_evidence_id: str,
    mutations: list[dict[str, Any]] | None = None,
    comparison: dict[str, Any] | None = None,
    **options: Any,
) -> ReplayRequestPayload:
    value = {
        "session_id": session_id,
        "objective": objective,
        "source": {
            "experiment_id": source_experiment_id,
            "evidence_id": source_evidence_id,
        },
        "mutations": mutations or [],
        **options,
    }
    if comparison is not None:
        value["comparison"] = comparison
    return ReplayRequestPayload.model_validate(value)


def control_preset(
    *,
    session_id: str,
    objective: str,
    source_experiment_id: str,
    source_evidence_id: str,
    **options: Any,
) -> ReplayRequestPayload:
    """Build a zero-mutation generic replay used as a baseline observation."""

    return _payload(
        session_id=session_id,
        objective=objective,
        source_experiment_id=source_experiment_id,
        source_evidence_id=source_evidence_id,
        mutations=[],
        **options,
    )


def exploratory_preset(
    *,
    session_id: str,
    objective: str,
    source_experiment_id: str,
    source_evidence_id: str,
    mutations: list[dict[str, Any]],
    **options: Any,
) -> ReplayRequestPayload:
    """Build a generic replay with zero or more exploratory mutations."""

    return _payload(
        session_id=session_id,
        objective=objective,
        source_experiment_id=source_experiment_id,
        source_evidence_id=source_evidence_id,
        mutations=mutations,
        **options,
    )


def treatment_preset(
    *,
    session_id: str,
    objective: str,
    source_experiment_id: str,
    source_evidence_id: str,
    references: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
    comparison_dimensions: list[str] | None = None,
    **options: Any,
) -> ReplayRequestPayload:
    """Build a generic replay that compares itself with explicit references."""

    comparison = options.pop(
        "comparison",
        {
            "references": references,
            "dimensions": comparison_dimensions or ["response_status"],
        },
    )
    return _payload(
        session_id=session_id,
        objective=objective,
        source_experiment_id=source_experiment_id,
        source_evidence_id=source_evidence_id,
        mutations=mutations,
        comparison=comparison,
        **options,
    )
