"""Privacy-safe local telemetry for Skill and Browser Action quality metrics."""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .browser.core import utc_now


class TelemetryRecorder:
    """Append bounded JSONL events without payloads, credentials, or evidence content."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "telemetry" / "action-events.jsonl"
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        safe = {
            "timestamp": utc_now(),
            "event": event,
            **{key: value for key, value in fields.items() if value is not None},
        }
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 16_384:
            raise ValueError("Telemetry event exceeds the bounded event size")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")


def load_events(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    event_counts = Counter(str(item.get("event") or "unknown") for item in events)
    skill_loads = [item for item in events if item.get("event") == "skill_load_completed"]
    browser_received = [
        item for item in events if item.get("event") == "browser_request_received"
    ]
    browser_valid = [item for item in events if item.get("event") == "browser_request_valid"]
    browser_errors = [item for item in events if item.get("event") == "browser_request_error"]
    validation_errors = [
        item for item in browser_errors if item.get("code") == "invalid_operation_payload"
    ]
    stale_errors = [
        item for item in browser_errors if item.get("code") == "stale_operation_contract"
    ]
    unknown_outcomes = [
        item
        for item in browser_errors
        if item.get("code") == "operation_outcome_unknown"
        or item.get("outcome") == "unknown"
    ]
    loaded_counts = [
        int(item.get("loaded_skill_count", 0))
        for item in skill_loads
        if isinstance(item.get("loaded_skill_count"), int)
    ]
    operation_counts = Counter(
        str(item.get("operation"))
        for item in browser_received
        if item.get("operation")
    )
    return {
        "format": "skill-temple-telemetry-summary-v1",
        "event_count": len(events),
        "event_counts": dict(sorted(event_counts.items())),
        "skill_metrics": {
            "load_requests": len(skill_loads),
            "average_loaded_skill_count": (
                sum(loaded_counts) / len(loaded_counts) if loaded_counts else 0.0
            ),
            "read_requests": event_counts.get("skill_read_completed", 0),
        },
        "browser_metrics": {
            "requests": len(browser_received),
            "valid_requests": len(browser_valid),
            "first_pass_valid_rate": (
                len(browser_valid) / len(browser_received) if browser_received else 0.0
            ),
            "validation_error_count": len(validation_errors),
            "stale_contract_count": len(stale_errors),
            "unknown_outcome_count": len(unknown_outcomes),
            "operation_counts": dict(sorted(operation_counts.items())),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize privacy-safe Action telemetry.")
    parser.add_argument("events", type=Path, help="Path to action-events.jsonl")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    rendered = json.dumps(
        summarize_events(load_events(args.events)),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")


if __name__ == "__main__":  # pragma: no cover
    main()
