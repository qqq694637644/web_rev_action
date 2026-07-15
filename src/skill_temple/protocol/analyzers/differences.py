"""Pure factual comparison helpers with no browser or persistence dependencies."""

from __future__ import annotations

from typing import Any


def compare_environment_facts(
    reference: dict[str, Any] | None,
    current: dict[str, Any] | None,
    dimensions: list[str],
) -> dict[str, Any]:
    """Compare only explicitly selected environment facts."""
    facts = {
        dimension: compare_dimension(
            reference.get(dimension) if isinstance(reference, dict) else None,
            current.get(dimension) if isinstance(current, dict) else None,
        )
        for dimension in dimensions
    }
    return {
        "status": aggregate_dimension_status(facts),
        "dimensions": facts,
    }


def stream_summary_from_observation(
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the narrow stream comparison summary from one canonical observation."""
    facts = observation.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    value = {
        "raw_event_count": facts.get("raw_event_count"),
        "semantic_event_count": facts.get("semantic_event_count"),
        "terminal_reason": facts.get("terminal_reason"),
        "primary_event_source": facts.get("primary_event_source"),
    }
    return value if any(item is not None for item in value.values()) else None


def select_current_stream_summary(
    observations: list[dict[str, Any]],
    replay_network_evidence_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Select the unique observation linked to the exact replay evidence."""
    if not replay_network_evidence_id:
        return None, "missing"
    matches = [
        item
        for item in observations
        if isinstance(item.get("sources"), dict)
        and item["sources"].get("network_evidence_id") == replay_network_evidence_id
    ]
    if not matches:
        return None, "missing"
    if len(matches) > 1:
        return None, "ambiguous"
    summary = stream_summary_from_observation(matches[0])
    return (summary, None) if summary is not None else (None, "missing")


def compare_dimension(
    reference: Any,
    current: Any,
    *,
    reference_status: str | None = None,
    current_status: str | None = None,
) -> dict[str, Any]:
    """Compare one factual dimension while preserving missing/ambiguous selection."""
    overrides = {item for item in (reference_status, current_status) if item}
    status = (
        "ambiguous"
        if "ambiguous" in overrides
        else "missing"
        if "missing" in overrides or reference is None or current is None
        else "equivalent"
        if reference == current
        else "different"
    )
    return {
        "status": status,
        "reference": reference,
        "current": current,
    }


def aggregate_dimension_status(dimensions: dict[str, Any]) -> str:
    """Aggregate dimension statuses without producing a causal verdict."""
    statuses = {
        item.get("status")
        for item in dimensions.values()
        if isinstance(item, dict)
    }
    return (
        "ambiguous"
        if "ambiguous" in statuses
        else "different"
        if "different" in statuses
        else "missing"
        if "missing" in statuses
        else "equivalent"
        if dimensions
        else "unknown"
    )
