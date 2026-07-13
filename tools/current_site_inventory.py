"""Generate current-site reconnaissance reports from browser experiment manifests.

The generator intentionally consumes only manifest-level facts. It does not read request bodies,
raw headers, stream payloads, screenshots, or credential artifacts. Missing observations are
reported as evidence gaps instead of being inferred from historical protocol assumptions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

REPORT_FILENAMES = (
    "current-site-inventory.md",
    "current-ui-map.md",
    "current-network-map.md",
    "open-questions.md",
)

_CREDENTIAL_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
}
_CREDENTIAL_HEADER_FRAGMENTS = ("csrf", "xsrf", "token", "session", "auth")
_IDENTIFIER_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:id|ids|uuid|nonce|cursor|parent|thread|conversation|message)(?:$|[/_.-])",
    re.IGNORECASE,
)


class InventoryError(RuntimeError):
    """Raised when reconnaissance reports cannot be generated."""


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _markdown(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    rendered = ["| " + " | ".join(_markdown(item) for item in headers) + " |"]
    rendered.append("| " + " | ".join("---" for _ in headers) + " |")
    row_count = 0
    for row in rows:
        rendered.append("| " + " | ".join(_markdown(item) for item in row) + " |")
        row_count += 1
    if row_count == 0:
        rendered.append("| " + " | ".join("—" for _ in headers) + " |")
    return "\n".join(rendered)


def _url_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    parsed = urlsplit(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    path = parsed.path or "/"
    query_names = sorted({name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    endpoint = f"{origin}{path}" if origin else path
    return {
        "raw": raw,
        "scheme": parsed.scheme.lower(),
        "origin": origin,
        "path": path,
        "query_names": query_names,
        "endpoint": endpoint,
    }


def _header_value(headers: Any, name: str) -> str | None:
    target = name.lower()
    for item in _dict_list(headers):
        if str(item.get("name", "")).lower() == target:
            value = str(item.get("value", "")).strip()
            return value or None
    return None


def _content_type(summary: dict[str, Any]) -> str | None:
    value = _header_value(summary.get("response_headers"), "content-type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _transport_label(summary: dict[str, Any], url_fact: dict[str, Any] | None) -> str:
    resource_type = str(summary.get("resource_type") or "").strip().lower()
    content_type = _content_type(summary)
    scheme = url_fact.get("scheme") if url_fact else ""
    if scheme in {"ws", "wss"} or resource_type == "websocket":
        return "WebSocket"
    if content_type == "text/event-stream":
        if resource_type == "eventsource":
            return "SSE (EventSource)"
        return "SSE over fetch/XHR"
    if content_type in {"application/x-ndjson", "application/ndjson"}:
        return "NDJSON"
    if content_type == "application/json-seq":
        return "JSON text sequence"
    if resource_type in {"fetch", "xhr"}:
        return resource_type.upper()
    if resource_type:
        return resource_type
    return "HTTP/unknown"


def _credential_header_names(summary: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in _dict_list(summary.get("request_headers")):
        name = str(item.get("name", "")).strip()
        normalized = name.lower()
        if normalized in _CREDENTIAL_HEADER_NAMES or any(
            fragment in normalized for fragment in _CREDENTIAL_HEADER_FRAGMENTS
        ):
            names.add(name)
    return names


def _request_identifier_paths(summary: dict[str, Any]) -> set[str]:
    shape = summary.get("request_shape")
    if not isinstance(shape, dict):
        return set()
    paths = shape.get("paths")
    if not isinstance(paths, dict):
        return set()
    return {str(path) for path in paths if _IDENTIFIER_PATH_RE.search(str(path))}


def load_manifests(
    evidence_root: Path,
    *,
    session_id: str | None = None,
    analysis_series_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load valid manifests matching the optional session and series filters."""

    evidence_root = evidence_root.expanduser().resolve()
    experiments_dir = evidence_root / "experiments"
    if not experiments_dir.is_dir():
        raise InventoryError(f"Experiments directory was not found: {experiments_dir}")

    manifests: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in sorted(experiments_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            skipped.append(f"{path.relative_to(evidence_root).as_posix()}: {type(exc).__name__}")
            continue
        if not isinstance(manifest, dict):
            skipped.append(f"{path.relative_to(evidence_root).as_posix()}: not an object")
            continue
        if session_id is not None and manifest.get("session_id") != session_id:
            continue
        series = manifest.get("series")
        series = series if isinstance(series, dict) else {}
        if (
            analysis_series_id is not None
            and series.get("analysis_series_id") != analysis_series_id
        ):
            continue
        manifests.append(manifest)

    manifests.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("experiment_id")),
        )
    )
    if not manifests:
        filters = []
        if session_id is not None:
            filters.append(f"session_id={session_id}")
        if analysis_series_id is not None:
            filters.append(f"analysis_series_id={analysis_series_id}")
        suffix = f" for {', '.join(filters)}" if filters else ""
        raise InventoryError(f"No valid experiment manifests were found{suffix}.")
    return manifests, skipped


def collect_facts(
    manifests: list[dict[str, Any]],
    *,
    skipped_manifests: list[str] | None = None,
) -> dict[str, Any]:
    """Collect bounded structural facts from experiment manifests."""

    facts: dict[str, Any] = {
        "experiments": [],
        "pages": [],
        "steps": [],
        "network": [],
        "streams": [],
        "evidence_kinds": Counter(),
        "query_names": set(),
        "credential_header_names": set(),
        "identifier_paths": set(),
        "worker_indicators": set(),
        "series_ids": set(),
        "skipped_manifests": list(skipped_manifests or []),
        "snapshot_path_count": 0,
        "console_message_count": 0,
    }

    page_keys: set[tuple[str, str, str, str]] = set()
    for manifest in manifests:
        experiment_id = str(manifest.get("experiment_id") or "unknown")
        series = manifest.get("series")
        series = series if isinstance(series, dict) else {}
        series_id = series.get("analysis_series_id")
        if series_id:
            facts["series_ids"].add(str(series_id))
        facts["experiments"].append(
            {
                "experiment_id": experiment_id,
                "session_id": manifest.get("session_id"),
                "operation": manifest.get("operation"),
                "status": manifest.get("status"),
                "created_at": manifest.get("created_at"),
                "objective": manifest.get("objective"),
                "series_id": series_id,
                "scenario_type": series.get("scenario_type"),
                "execution_integrity": manifest.get("execution_integrity"),
                "evidence_integrity": manifest.get("evidence_integrity"),
            }
        )

        for alignment_name in ("page_alignment", "post_flow_alignment"):
            alignment = manifest.get(alignment_name)
            alignment = alignment if isinstance(alignment, dict) else {}
            candidates = []
            playwright_page = alignment.get("playwright_page")
            if isinstance(playwright_page, dict):
                candidates.append(
                    (
                        playwright_page.get("url"),
                        playwright_page.get("title"),
                        alignment.get("status"),
                        f"{alignment_name}.playwright",
                    )
                )
            candidates.append(
                (
                    alignment.get("js_reverse_page_url"),
                    None,
                    alignment.get("status"),
                    f"{alignment_name}.js-reverse",
                )
            )
            for url, title, status, source in candidates:
                url_fact = _url_fact(url)
                if url_fact is None:
                    continue
                key = (experiment_id, url_fact["endpoint"], str(title or ""), source)
                if key in page_keys:
                    continue
                page_keys.add(key)
                facts["pages"].append(
                    {
                        "experiment_id": experiment_id,
                        "source": source,
                        "title": title,
                        "alignment_status": status,
                        **url_fact,
                    }
                )
                facts["query_names"].update(url_fact["query_names"])

        for step in _dict_list(manifest.get("steps")):
            facts["steps"].append(
                {
                    "experiment_id": experiment_id,
                    "step_id": step.get("step_id"),
                    "status": step.get("status"),
                    "snapshot_ref": step.get("snapshot_ref"),
                    "error": step.get("error"),
                }
            )
        snapshot_paths = manifest.get("snapshot_paths")
        if isinstance(snapshot_paths, list):
            facts["snapshot_path_count"] += len(snapshot_paths)

        for evidence in _dict_list(manifest.get("evidence")):
            kind = str(evidence.get("kind") or "unknown")
            facts["evidence_kinds"][kind] += 1
            if kind == "console_message":
                facts["console_message_count"] += 1
            if "worker" in kind.lower():
                facts["worker_indicators"].add(kind)
            summary = evidence.get("summary")
            summary = summary if isinstance(summary, dict) else {}
            if kind == "network_request":
                url_fact = _url_fact(summary.get("url"))
                facts["query_names"].update(url_fact["query_names"] if url_fact else [])
                facts["credential_header_names"].update(_credential_header_names(summary))
                facts["identifier_paths"].update(_request_identifier_paths(summary))
                resource_type = str(summary.get("resource_type") or "")
                if "worker" in resource_type.lower():
                    facts["worker_indicators"].add(resource_type)
                safe_url_fact = url_fact or {
                    "endpoint": "",
                    "origin": "",
                    "path": "",
                    "query_names": [],
                }
                facts["network"].append(
                    {
                        "experiment_id": experiment_id,
                        "evidence_id": evidence.get("evidence_id"),
                        "selector_id": evidence.get("selector_id"),
                        "method": summary.get("method"),
                        "status": summary.get("status"),
                        "resource_type": summary.get("resource_type"),
                        "content_type": _content_type(summary),
                        "transport": _transport_label(summary, url_fact),
                        "snapshot_integrity": (
                            summary.get("snapshot_integrity")
                            if isinstance(summary.get("snapshot_integrity"), dict)
                            else {}
                        ),
                        **safe_url_fact,
                    }
                )
            elif kind == "stream_request":
                url_fact = _url_fact(summary.get("url"))
                facts["query_names"].update(url_fact["query_names"] if url_fact else [])
                safe_url_fact = url_fact or {
                    "endpoint": "",
                    "origin": "",
                    "path": "",
                    "query_names": [],
                }
                facts["streams"].append(
                    {
                        "experiment_id": experiment_id,
                        "evidence_id": evidence.get("evidence_id"),
                        "method": summary.get("method"),
                        "status": summary.get("status"),
                        "terminal_reason": summary.get("terminal_reason"),
                        "primary_event_source": summary.get("primary_event_source"),
                        "raw_event_count": summary.get("raw_event_count"),
                        "semantic_event_count": summary.get("semantic_event_count"),
                        "raw_capture_integrity": summary.get("raw_capture_integrity"),
                        "semantic_parse_integrity": summary.get("semantic_parse_integrity"),
                        "stream_artifact_integrity": summary.get("stream_artifact_integrity"),
                        **safe_url_fact,
                    }
                )

    return facts


def render_current_site_inventory(facts: dict[str, Any]) -> str:
    origins = sorted(
        {item["origin"] for item in [*facts["pages"], *facts["network"]] if item.get("origin")}
    )
    transports = sorted({item["transport"] for item in facts["network"]})
    evidence_kinds = facts["evidence_kinds"]
    evidence_rows = sorted(evidence_kinds.items())
    origins_text = ", ".join(origins) if origins else "none"
    credential_headers = sorted(facts["credential_header_names"])
    credential_headers_text = ", ".join(credential_headers) if credential_headers else "none"
    lines = [
        "# Current Site Inventory",
        "",
        "> Generated only from browser experiment manifests. Missing facts remain unknown; "
        "historical Pandora behavior is not used as a default.",
        "",
        "## Evidence scope",
        "",
        _table(
            [
                "Experiment",
                "Session",
                "Operation",
                "Scenario",
                "Status",
                "Execution",
                "Evidence",
            ],
            (
                (
                    item["experiment_id"],
                    item["session_id"],
                    item["operation"],
                    item["scenario_type"],
                    item["status"],
                    item["execution_integrity"],
                    item["evidence_integrity"],
                )
                for item in facts["experiments"]
            ),
        ),
        "",
        "## Observed inventory",
        "",
        f"- Origins observed in page or network facts: {origins_text}.",
        f"- Network transports represented by evidence: "
        f"{', '.join(transports) if transports else 'none'}.",
        f"- Stream request evidence entries: {len(facts['streams'])}.",
        f"- Page observations: {len(facts['pages'])}; step results: {len(facts['steps'])}.",
        f"- Page snapshot paths: {facts['snapshot_path_count']}; "
        f"console message evidence: {facts['console_message_count']}.",
        f"- Analysis series: "
        f"{', '.join(sorted(facts['series_ids'])) if facts['series_ids'] else 'not declared'}.",
        "",
        "## Evidence kinds",
        "",
        _table(["Kind", "Count"], evidence_rows),
        "",
        "## Structural state indicators",
        "",
        f"- Credential-related request header names observed: {credential_headers_text}.",
        f"- Query parameter names observed: "
        f"{', '.join(sorted(facts['query_names'])) if facts['query_names'] else 'none'}.",
        f"- Identifier-like JSON Pointer paths observed: "
        f"{', '.join(sorted(facts['identifier_paths'])) if facts['identifier_paths'] else 'none'}.",
        "",
        "## Scope limits",
        "",
        "- The generator does not read raw bodies, raw headers, stream payloads, screenshots, "
        "or credential artifacts.",
        "- Header names, endpoint paths, query names, request-shape paths, and integrity fields "
        "are inventory facts; their semantics are not inferred.",
    ]
    if facts["skipped_manifests"]:
        lines.extend(
            [
                "- Invalid or unreadable manifests were skipped:",
                *[f"  - `{item}`" for item in facts["skipped_manifests"]],
            ]
        )
    return "\n".join(lines) + "\n"


def render_current_ui_map(facts: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Current UI Map",
            "",
            "> This map contains only page alignment and step-result facts stored in manifests. "
            "It does not guess controls from historical UI flows.",
            "",
            "## Page observations",
            "",
            _table(
                ["Experiment", "Source", "Origin + path", "Query names", "Title", "Alignment"],
                (
                    (
                        item["experiment_id"],
                        item["source"],
                        item["endpoint"],
                        ", ".join(item["query_names"]),
                        item["title"],
                        item["alignment_status"],
                    )
                    for item in facts["pages"]
                ),
            ),
            "",
            "## Recorded step outcomes",
            "",
            _table(
                ["Experiment", "Step", "Status", "Snapshot reference", "Error"],
                (
                    (
                        item["experiment_id"],
                        item["step_id"],
                        item["status"],
                        item["snapshot_ref"],
                        item["error"],
                    )
                    for item in facts["steps"]
                ),
            ),
            "",
            "## Not established by these manifests",
            "",
            "- Interactive control roles, labels, and stable selectors.",
            "- iframe, shadow DOM, virtual-list, feature-flag, captcha, or regional behavior.",
            "- localStorage, sessionStorage, IndexedDB, and cookie categories.",
            "",
        ]
    )


def render_current_network_map(facts: dict[str, Any]) -> str:
    terminal_reasons = sorted(
        {str(item["terminal_reason"]) for item in facts["streams"] if item.get("terminal_reason")}
    )
    return "\n".join(
        [
            "# Current Network Map",
            "",
            "> Endpoints show origin and path plus query parameter names. Query values and "
            "message bodies are not loaded by this report generator.",
            "",
            "## Ordinary network evidence",
            "",
            _table(
                [
                    "Experiment",
                    "Evidence",
                    "Method",
                    "Endpoint",
                    "Query names",
                    "Resource type",
                    "Content type",
                    "Transport",
                    "Status",
                ],
                (
                    (
                        item["experiment_id"],
                        item["evidence_id"],
                        item["method"],
                        item["endpoint"],
                        ", ".join(item["query_names"]),
                        item["resource_type"],
                        item["content_type"],
                        item["transport"],
                        item["status"],
                    )
                    for item in facts["network"]
                ),
            ),
            "",
            "## Stream evidence",
            "",
            _table(
                [
                    "Experiment",
                    "Evidence",
                    "Endpoint",
                    "Event source",
                    "Terminal reason",
                    "Raw events",
                    "Semantic events",
                    "Raw integrity",
                    "Semantic integrity",
                    "Artifact integrity",
                ],
                (
                    (
                        item["experiment_id"],
                        item["evidence_id"],
                        item["endpoint"],
                        item["primary_event_source"],
                        item["terminal_reason"],
                        item["raw_event_count"],
                        item["semantic_event_count"],
                        item["raw_capture_integrity"],
                        item["semantic_parse_integrity"],
                        item["stream_artifact_integrity"],
                    )
                    for item in facts["streams"]
                ),
            ),
            "",
            "## Observed termination facts",
            "",
            f"- Terminal reasons present in stream evidence: "
            f"{', '.join(terminal_reasons) if terminal_reasons else 'none'}.",
            "- A missing WebSocket or worker row means only that the selected manifests contain "
            "no such evidence; it does not prove absence on the current site.",
            "",
        ]
    )


def render_open_questions(facts: dict[str, Any]) -> str:
    transports = sorted({item["transport"] for item in facts["network"]})
    terminal_reasons = sorted(
        {str(item["terminal_reason"]) for item in facts["streams"] if item.get("terminal_reason")}
    )
    has_websocket = "WebSocket" in transports
    has_script_source = facts["evidence_kinds"].get("script_source", 0) > 0
    questions = [
        (
            "UI structure",
            f"{len(facts['pages'])} page observations and {facts['snapshot_path_count']} "
            "snapshot paths are present.",
            "Control roles, iframe/shadow DOM, virtual lists, captcha, and feature flags are not "
            "encoded as inventory facts.",
            "Inspect bounded page snapshot artifacts and record stable control facts.",
        ),
        (
            "Transport",
            f"Observed labels: {', '.join(transports) if transports else 'none'}.",
            "Unobserved transports remain possible outside the selected experiment windows.",
            "Capture initialization and one representative interaction with broad "
            "network evidence.",
        ),
        (
            "Stream termination",
            "Observed terminal reasons: "
            f"{', '.join(terminal_reasons) if terminal_reasons else 'none'}.",
            "Network close, protocol event, state field, and page-state termination are not "
            "distinguished unless explicit stream evidence records them.",
            "Capture one complete stream and one deliberate interruption with "
            "explicit checkpoints.",
        ),
        (
            "Authentication",
            "Credential-related header names: "
            + (
                ", ".join(sorted(facts["credential_header_names"]))
                if facts["credential_header_names"]
                else "none observed"
            )
            + ".",
            "Header names do not establish credential origin, rotation, cookie scope, CSRF flow, "
            "or storage source.",
            "Compare login, refresh, and re-attach experiments without copying credential values.",
        ),
        (
            "Dynamic identifiers",
            "Identifier-like request paths: "
            + (
                ", ".join(sorted(facts["identifier_paths"]))
                if facts["identifier_paths"]
                else "none observed"
            )
            + ".",
            "The manifests do not prove whether values are generated by the client, server, "
            "setup response, worker, or page runtime.",
            "Trace request initiators and compare values across isolated captures.",
        ),
        (
            "Worker coverage",
            "Worker indicators: "
            + (
                ", ".join(sorted(facts["worker_indicators"]))
                if facts["worker_indicators"]
                else "none observed"
            )
            + ".",
            "No indicator is not proof that Service Worker, Web Worker, or Shared Worker "
            "is absent.",
            "Inspect registrations, targets, and initiator chains during initialization "
            "and dispatch.",
        ),
        (
            "WebSocket coverage",
            (
                "WebSocket evidence is present."
                if has_websocket
                else "No WebSocket evidence is present."
            ),
            "The selected captures may not cover initialization, reconnect, or "
            "background channels.",
            "Capture from navigation start and inspect socket creation and frame evidence "
            "if present.",
        ),
        (
            "Source construction",
            "Saved script-source evidence is present."
            if has_script_source
            else "No saved script-source evidence is present.",
            "Request builders, stream parsers, reducers, and source-map availability remain "
            "unknown without initiator-linked source evidence.",
            "Save bounded source regions linked to concrete network evidence IDs.",
        ),
        (
            "Refresh and account-state differences",
            f"The report includes {len(facts['experiments'])} experiment manifests.",
            "The selected facts do not by themselves establish behavior after reload, re-login, "
            "session rotation, retry, or regional/feature-flag changes.",
            "Run explicitly labeled comparison captures and regenerate these reports with "
            "one series filter.",
        ),
    ]
    return "\n".join(
        [
            "# Open Questions",
            "",
            "> Questions are generated from missing or incomplete manifest facts. They are not "
            "protocol conclusions.",
            "",
            _table(["Area", "Observed fact", "Evidence gap", "Next evidence"], questions),
            "",
        ]
    )


def generate_reports(
    evidence_root: Path,
    output_dir: Path,
    *,
    session_id: str | None = None,
    analysis_series_id: str | None = None,
) -> dict[str, Path]:
    manifests, skipped = load_manifests(
        evidence_root,
        session_id=session_id,
        analysis_series_id=analysis_series_id,
    )
    facts = collect_facts(manifests, skipped_manifests=skipped)
    rendered = {
        "current-site-inventory.md": render_current_site_inventory(facts),
        "current-ui-map.md": render_current_ui_map(facts),
        "current-network-map.md": render_current_network_map(facts),
        "open-questions.md": render_open_questions(facts),
    }
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for filename in REPORT_FILENAMES:
        path = output_dir / filename
        path.write_text(rendered[filename], encoding="utf-8", newline="\n")
        paths[filename] = path
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate current-site reconnaissance reports from experiment manifests."
    )
    parser.add_argument(
        "evidence_root",
        type=Path,
        help="Evidence root containing experiments/<experiment_id>/manifest.json.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports"),
        help="Output directory for the four Markdown reports (default: reports).",
    )
    parser.add_argument("--session-id", help="Include only manifests from this browser session.")
    parser.add_argument(
        "--analysis-series-id",
        help="Include only manifests from this explicit analysis series.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = generate_reports(
            args.evidence_root,
            args.output_dir,
            session_id=args.session_id,
            analysis_series_id=args.analysis_series_id,
        )
    except InventoryError as exc:
        print(f"current-site inventory failed: {exc}", file=sys.stderr)
        return 2
    for filename in REPORT_FILENAMES:
        print(paths[filename])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
