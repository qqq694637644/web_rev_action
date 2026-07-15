"""Ordered replay binding and mutation execution with final-wire audit."""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from ..browser_models import ReplayBinding, ReplayMutation
from .shapes import decode_json_request_body, public_body_summary
from .values import (
    _decode_pointer,
    _last_pointer_token,
    _redact_json_value,
)

_BROWSER_MANAGED_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def redacted_mutation(mutation: ReplayMutation) -> dict[str, Any]:
    value = mutation.model_dump(mode="json")
    if "value" in value:
        value["value"] = _redact_json_value(
            value["value"],
            key_hint=(value.get("name") or _last_pointer_token(str(value.get("path", "")))),
        )
    return value

def binding_value_from_snapshot(
    snapshot: dict[str, Any],
    binding: ReplayBinding,
) -> Any:
    if binding.target == "json_pointer":
        body = decode_json_request_body(snapshot.get("requestBody"))
        if body is None:
            raise ValueError("preserve_source JSON binding requires an available JSON body")
        exists, value = _read_pointer(body, str(binding.path))
        if not exists:
            raise ValueError(f"preserve_source binding path is missing: {binding.path}")
        return copy.deepcopy(value)
    if binding.target == "header":
        values = [
            item["value"]
            for item in _normalized_headers(snapshot.get("requestHeadersArray"))
            if item["name"].lower() == str(binding.name).lower()
        ]
        if not values:
            raise ValueError(f"preserve_source binding header is missing: {binding.name}")
        if binding.occurrence >= len(values):
            raise ValueError(
                f"preserve_source binding header occurrence is missing: "
                f"{binding.name}[{binding.occurrence}]"
            )
        return values[binding.occurrence]
    values = [
        value
        for name, value in parse_qsl(
            urlsplit(str(snapshot.get("url", ""))).query,
            keep_blank_values=True,
        )
        if name == binding.name
    ]
    if not values:
        raise ValueError(f"preserve_source query parameter is missing: {binding.name}")
    if binding.occurrence >= len(values):
        raise ValueError(
            f"preserve_source query occurrence is missing: {binding.name}[{binding.occurrence}]"
        )
    return values[binding.occurrence]

def json_pointer_value(document: Any, path: str) -> Any:
    exists, value = _read_pointer(document, path)
    if not exists:
        raise ValueError(f"JSON Pointer path does not exist: {path}")
    return copy.deepcopy(value)

def build_replay_spec(
    snapshot: dict[str, Any],
    mutations: list[ReplayMutation],
    *,
    bindings: list[ReplayBinding] | None = None,
    binding_values: dict[str, Any] | None = None,
    query_serialization: str = "preserve_raw",
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = str(snapshot.get("url", ""))
    method = str(snapshot.get("method", "GET")).upper()
    headers = _normalized_headers(snapshot.get("requestHeadersArray"))
    body = copy.deepcopy(snapshot.get("requestBody"))
    source_url = url
    source_headers = copy.deepcopy(headers)
    source_body = copy.deepcopy(body)
    ignored_headers: list[str] = []

    for binding in bindings or []:
        if binding_values is None or binding.binding_id not in binding_values:
            raise ValueError(f"Missing binding value: {binding.binding_id}")
        binding_value = binding_values[binding.binding_id]
        if binding.target == "json_pointer":
            body = _replace_json_pointer(body, str(binding.path), binding_value)
        elif binding.target == "header":
            headers = _replace_header(
                headers,
                str(binding.name),
                str(binding_value),
                occurrence=binding.occurrence,
            )
        else:
            url = _mutate_query(
                url,
                str(binding.name),
                str(binding_value),
                operation="replace",
                occurrence=binding.occurrence,
                serialization=query_serialization,
            )

    for mutation in mutations:
        if mutation.type in {"remove_header", "replace_header", "add_header"}:
            headers = _mutate_headers(headers, mutation)
        elif mutation.type in {
            "remove_query_parameter",
            "replace_query_parameter",
            "add_query_parameter",
        }:
            operation = mutation.type.split("_", 1)[0]
            url = _mutate_query(
                url,
                mutation.name,
                getattr(mutation, "value", None),
                operation=operation,
                occurrence=mutation.occurrence,
                serialization=query_serialization,
            )
        elif mutation.type in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }:
            body = _mutate_json_body(body, mutation)

    replay_headers: list[dict[str, str]] = []
    for item in headers:
        normalized = item["name"].lower()
        if normalized in _BROWSER_MANAGED_HEADERS or normalized.startswith("sec-"):
            ignored_headers.append(item["name"])
            continue
        replay_headers.append(item)

    spec = {
        "url": url,
        "method": method,
        "headers": replay_headers,
        "body": body if isinstance(body, dict) and body.get("available") else None,
        "querySerialization": query_serialization,
    }
    diff = {
        "source": {
            "url": source_url[:8192],
            "method": method,
            "header_names": [item["name"] for item in source_headers],
            "request_body": public_body_summary(source_body),
        },
        "replay": {
            "url": url[:8192],
            "method": method,
            "header_names": [item["name"] for item in replay_headers],
            "ignored_browser_managed_headers": sorted(set(ignored_headers)),
            "request_body": public_body_summary(body),
            "query_serialization": query_serialization,
        },
        "mutations": [redacted_mutation(mutation) for mutation in mutations],
        "bindings": [
            {
                "binding_id": binding.binding_id,
                "target": binding.target,
                "path": binding.path,
                "name": binding.name,
                "generator": binding.generator,
                "value_source": binding.value_source,
                "value": "<bound>",
            }
            for binding in bindings or []
        ],
    }
    return spec, diff

def _replay_operation_target(operation: ReplayBinding | ReplayMutation) -> dict[str, Any]:
    if isinstance(operation, ReplayBinding):
        return {
            "kind": operation.target,
            "name": (
                str(operation.path)
                if operation.target == "json_pointer"
                else str(operation.name).lower()
                if operation.target == "header"
                else str(operation.name)
            ),
            "occurrence": operation.occurrence,
            "action": "binding",
        }
    if operation.type.endswith("json_path"):
        return {
            "kind": "json_pointer",
            "name": str(operation.path),
            "occurrence": None,
            "action": operation.type.split("_", 1)[0],
        }
    kind = "header" if operation.type.endswith("header") else "query_parameter"
    return {
        "kind": kind,
        "name": (
            str(operation.name).lower() if kind == "header" else str(operation.name)
        ),
        "occurrence": operation.occurrence,
        "action": operation.type.split("_", 1)[0],
    }

def replay_operation_overwritten_by_later(
    operation: ReplayBinding | ReplayMutation,
    later_operation: ReplayMutation,
) -> bool:
    current = _replay_operation_target(operation)
    later = _replay_operation_target(later_operation)
    if current["kind"] != later["kind"] or later["action"] == "add":
        return False
    if current["kind"] == "json_pointer":
        current_tokens = _decode_pointer(str(current["name"]))
        later_tokens = _decode_pointer(str(later["name"]))
        return (
            current_tokens[: len(later_tokens)] == later_tokens
            or later_tokens[: len(current_tokens)] == current_tokens
        )
    if current["name"] != later["name"]:
        return False
    later_occurrence = later["occurrence"]
    current_occurrence = current["occurrence"]
    return later_occurrence == "all" or (
        isinstance(current_occurrence, int)
        and isinstance(later_occurrence, int)
        and current_occurrence == later_occurrence
    )

def assess_mutation_effectiveness(
    mutation: ReplayMutation | None,
    wire_snapshot: dict[str, Any] | None,
    *,
    overwritten_by_later: bool = False,
) -> dict[str, Any]:
    if mutation is None:
        return {
            "mutation_requested": None,
            "mutation_observed_on_wire": None,
            "mutation_effective": None,
            "reason": "replay has no mutation to observe",
        }
    requested = redacted_mutation(mutation)
    if overwritten_by_later:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": None,
            "final_wire_observability": "overwritten_by_later_operation",
            "reason": (
                "mutation was applied in order but its intermediate value was "
                "overwritten by a later operation"
            ),
        }
    if wire_snapshot is None:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
            "final_wire_observability": "unavailable",
            "reason": "exact replay request snapshot was not exported",
        }
    try:
        if mutation.type in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }:
            body_value = decode_json_request_body(wire_snapshot.get("requestBody"))
            if body_value is None:
                raise ValueError("wire request has no JSON body")
            exists, observed = _read_pointer(body_value, mutation.path)
            effective = (
                not exists
                if mutation.type == "remove_json_path"
                else exists and observed == mutation.value
            )
            observed_public = (
                "<absent>"
                if not exists
                else _redact_json_value(observed, key_hint=_last_pointer_token(mutation.path))
            )
        elif mutation.type in {"remove_header", "replace_header", "add_header"}:
            _, values = _observe_mutation_target(wire_snapshot, mutation)
            effective = (
                not values
                if mutation.type == "remove_header"
                else _occurrence_value_matches(
                    values,
                    mutation.value,
                    mutation.occurrence,
                )
            )
            observed_public = "<absent>" if not values else "<present>"
        else:
            _, values = _observe_mutation_target(wire_snapshot, mutation)
            effective = (
                not values
                if mutation.type == "remove_query_parameter"
                else _occurrence_value_matches(
                    values,
                    mutation.value,
                    mutation.occurrence,
                )
            )
            observed_public = "<absent>" if not values else "<present>"
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": observed_public,
            "mutation_effective": effective,
            "final_wire_observability": "observed" if effective else "contradicted",
            "reason": (
                "wire request matches requested mutation"
                if effective
                else "wire request does not match requested mutation"
            ),
        }
    except ValueError as exc:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
            "final_wire_observability": "unavailable",
            "reason": str(exc),
        }

def observe_binding_application(
    wire_snapshot: dict[str, Any] | None,
    *,
    bindings: list[ReplayBinding],
    binding_values: dict[str, Any],
    mutations: list[ReplayMutation] | None = None,
) -> dict[str, Any]:
    mutations = mutations or []
    if wire_snapshot is None:
        return {
            "binding_count": len(bindings),
            "binding_application_complete": False,
            "binding_observations": [
                {
                    "binding_id": binding.binding_id,
                    "operation_applied_to_spec": True,
                    "final_wire_observability": "unavailable",
                }
                for binding in bindings
            ],
            "reason": "exact replay request snapshot is required",
        }
    observations: list[dict[str, Any]] = []
    for binding in bindings:
        overwritten = any(
            replay_operation_overwritten_by_later(binding, mutation)
            for mutation in mutations
        )
        if overwritten:
            observations.append(
                {
                    "binding_id": binding.binding_id,
                    "operation_applied_to_spec": True,
                    "binding_observed_on_final_wire": None,
                    "final_wire_observability": "overwritten_by_later_operation",
                }
            )
            continue
        visible = _bindings_match_snapshot(
            wire_snapshot,
            [binding],
            binding_values,
        )
        observations.append(
            {
                "binding_id": binding.binding_id,
                "operation_applied_to_spec": True,
                "binding_observed_on_final_wire": visible,
                "final_wire_observability": "observed" if visible else "contradicted",
            }
        )
    bindings_effective = all(
        item["final_wire_observability"]
        in {"observed", "overwritten_by_later_operation"}
        for item in observations
    )
    return {
        "binding_count": len(bindings),
        "binding_application_complete": bindings_effective,
        "binding_observations": observations,
        "reason": (
            "all resolved bindings were applied; visible values match the final wire"
            if bindings_effective
            else "one or more resolved bindings contradict the final wire request"
        ),
    }

def _observe_mutation_target(
    snapshot: dict[str, Any],
    mutation: ReplayMutation,
) -> tuple[bool, Any]:
    if mutation.type in {
        "remove_json_path",
        "replace_json_path",
        "add_json_path",
    }:
        body = decode_json_request_body(snapshot.get("requestBody"))
        if body is None:
            raise ValueError("wire request has no JSON body")
        return _read_pointer(body, mutation.path)
    if mutation.type in {"remove_header", "replace_header", "add_header"}:
        values = [
            item["value"]
            for item in _normalized_headers(snapshot.get("requestHeadersArray"))
            if item["name"].lower() == mutation.name.lower()
        ]
        return bool(values), values
    values = [
        value
        for name, value in parse_qsl(
            urlsplit(str(snapshot.get("url", ""))).query,
            keep_blank_values=True,
        )
        if name == mutation.name
    ]
    return bool(values), values

def _bindings_match_snapshot(
    snapshot: dict[str, Any],
    bindings: list[ReplayBinding],
    expected_values: dict[str, Any],
) -> bool:
    for binding in bindings:
        if binding.binding_id not in expected_values:
            return False
        expected = expected_values[binding.binding_id]
        if binding.target == "json_pointer":
            body = decode_json_request_body(snapshot.get("requestBody"))
            if body is None:
                return False
            exists, observed = _read_pointer(body, str(binding.path))
        elif binding.target == "header":
            values = [
                item["value"]
                for item in _normalized_headers(snapshot.get("requestHeadersArray"))
                if item["name"].lower() == str(binding.name).lower()
            ]
            exists = binding.occurrence < len(values)
            observed = values[binding.occurrence] if exists else None
            expected = str(expected)
        else:
            values = [
                value
                for name, value in parse_qsl(
                    urlsplit(str(snapshot.get("url", ""))).query,
                    keep_blank_values=True,
                )
                if name == binding.name
            ]
            exists = binding.occurrence < len(values)
            observed = values[binding.occurrence] if exists else None
            expected = str(expected)
        if not exists or observed != expected:
            return False
    return True

def _normalized_headers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"name": str(item.get("name", "")), "value": str(item.get("value", ""))}
        for item in value
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]

def _replace_header(
    headers: list[dict[str, str]],
    name: str,
    value: str,
    *,
    occurrence: int = 0,
) -> list[dict[str, str]]:
    result = copy.deepcopy(headers)
    matches = [index for index, item in enumerate(result) if item["name"].lower() == name.lower()]
    if occurrence >= len(matches):
        raise ValueError(f"Header occurrence does not exist: {name}[{occurrence}]")
    result[matches[occurrence]] = {"name": name, "value": value}
    return result

def _mutate_headers(
    headers: list[dict[str, str]],
    mutation: ReplayMutation,
) -> list[dict[str, str]]:
    result = copy.deepcopy(headers)
    matches = [
        index for index, item in enumerate(result) if item["name"].lower() == mutation.name.lower()
    ]
    occurrence = mutation.occurrence
    if mutation.type == "add_header":
        result.append({"name": mutation.name, "value": mutation.value})
        return result
    if occurrence == "all":
        if mutation.type == "remove_header":
            return [item for item in result if item["name"].lower() != mutation.name.lower()]
        if not matches:
            raise ValueError(f"Header does not exist: {mutation.name}")
        for index in matches:
            result[index] = {"name": mutation.name, "value": mutation.value}
        return result
    if occurrence >= len(matches):
        raise ValueError(f"Header occurrence does not exist: {mutation.name}[{occurrence}]")
    target_index = matches[occurrence]
    if mutation.type == "remove_header":
        del result[target_index]
    else:
        result[target_index] = {"name": mutation.name, "value": mutation.value}
    return result

def _mutate_query(
    url: str,
    name: str,
    value: str | None,
    *,
    operation: str,
    occurrence: int | str,
    serialization: str = "preserve_raw",
) -> str:
    split = urlsplit(url)
    if serialization == "preserve_raw":
        raw_entries = split.query.split("&") if split.query else []
        decoded_names = [
            unquote_plus(item.partition("=")[0])
            for item in raw_entries
        ]
        matches = [index for index, key in enumerate(decoded_names) if key == name]
        encoded_pair = urlencode([(name, value or "")])
        if operation == "add":
            raw_entries.append(encoded_pair)
        elif occurrence == "all":
            if operation == "remove":
                raw_entries = [
                    item
                    for index, item in enumerate(raw_entries)
                    if index not in set(matches)
                ]
            else:
                if not matches:
                    raise ValueError(f"Query parameter does not exist: {name}")
                for index in matches:
                    raw_key = raw_entries[index].partition("=")[0]
                    raw_entries[index] = f"{raw_key}={encoded_pair.partition('=')[2]}"
        else:
            selected = int(occurrence)
            if selected >= len(matches):
                raise ValueError(f"Query occurrence does not exist: {name}[{selected}]")
            target_index = matches[selected]
            if operation == "remove":
                del raw_entries[target_index]
            else:
                raw_key = raw_entries[target_index].partition("=")[0]
                raw_entries[target_index] = f"{raw_key}={encoded_pair.partition('=')[2]}"
        return urlunsplit(
            (
                split.scheme,
                split.netloc,
                split.path,
                "&".join(raw_entries),
                split.fragment,
            )
        )
    if serialization != "normalize":
        raise ValueError(f"Unsupported query serialization mode: {serialization}")
    entries = list(parse_qsl(split.query, keep_blank_values=True))
    matches = [index for index, (key, _) in enumerate(entries) if key == name]
    if operation == "add":
        entries.append((name, value or ""))
    elif occurrence == "all":
        if operation == "remove":
            entries = [(key, item) for key, item in entries if key != name]
        else:
            if not matches:
                raise ValueError(f"Query parameter does not exist: {name}")
            for index in matches:
                entries[index] = (name, value or "")
    else:
        selected = int(occurrence)
        if selected >= len(matches):
            raise ValueError(f"Query occurrence does not exist: {name}[{selected}]")
        target_index = matches[selected]
        if operation == "remove":
            del entries[target_index]
        else:
            entries[target_index] = (name, value or "")
    return urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urlencode(entries, doseq=True),
            split.fragment,
        )
    )

def _mutate_json_body(body: Any, mutation: ReplayMutation) -> dict[str, Any]:
    if not isinstance(body, dict) or not body.get("available"):
        raise ValueError("JSON mutation requires an available request body")
    if body.get("encoding") != "utf8":
        raise ValueError("JSON mutation requires a UTF-8 request body")
    try:
        value = json.loads(str(body.get("text", "")))
    except json.JSONDecodeError as exc:
        raise ValueError("JSON mutation requires a valid JSON request body") from exc
    path = str(getattr(mutation, "path", ""))
    if mutation.type == "remove_json_path":
        _remove_pointer(value, path)
    elif mutation.type == "add_json_path":
        _add_pointer(value, path, mutation.value)
    else:
        _replace_pointer(value, path, mutation.value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "available": True,
        "size": len(encoded.encode("utf-8")),
        "encoding": "utf8",
        "text": encoded,
    }

def _replace_json_pointer(body: Any, path: str, value: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not body.get("available"):
        raise ValueError("JSON volatile binding requires an available request body")
    if body.get("encoding") != "utf8":
        raise ValueError("JSON volatile binding requires a UTF-8 request body")
    try:
        decoded = json.loads(str(body.get("text", "")))
    except json.JSONDecodeError as exc:
        raise ValueError("JSON volatile binding requires a valid JSON request body") from exc
    _replace_pointer(decoded, path, value)
    encoded = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    return {
        "available": True,
        "size": len(encoded.encode("utf-8")),
        "encoding": "utf8",
        "text": encoded,
    }

def _resolve_parent(document: Any, path: str) -> tuple[Any, str]:
    tokens = _decode_pointer(path)
    parent = document
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            if token not in parent:
                raise ValueError(f"JSON Pointer path does not exist: {path}")
            parent = parent[token]
        elif isinstance(parent, list):
            index = _parse_list_index(token, len(parent), path)
            parent = parent[index]
        else:
            raise ValueError(f"JSON Pointer traverses a scalar value: {path}")
    return parent, tokens[-1]

def _parse_list_index(token: str, length: int, path: str) -> int:
    if not token.isdigit():
        raise ValueError(f"JSON Pointer array token must be a non-negative index: {path}")
    index = int(token)
    if index >= length:
        raise ValueError(f"JSON Pointer array index is out of range: {path}")
    return index

def _read_pointer(document: Any, path: str) -> tuple[bool, Any]:
    try:
        parent, leaf = _resolve_parent(document, path)
        if isinstance(parent, dict):
            return (leaf in parent, parent.get(leaf))
        if isinstance(parent, list):
            index = _parse_list_index(leaf, len(parent), path)
            return True, parent[index]
        return False, None
    except ValueError:
        return False, None

def _remove_pointer(document: Any, path: str) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if leaf not in parent:
            raise ValueError(f"JSON Pointer path does not exist: {path}")
        del parent[leaf]
        return
    if isinstance(parent, list):
        del parent[_parse_list_index(leaf, len(parent), path)]
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")

def _replace_pointer(document: Any, path: str, value: Any) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if leaf not in parent:
            raise ValueError(f"JSON Pointer path does not exist: {path}")
        parent[leaf] = value
        return
    if isinstance(parent, list):
        parent[_parse_list_index(leaf, len(parent), path)] = value
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")

def _add_pointer(document: Any, path: str, value: Any) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        parent[leaf] = value
        return
    if isinstance(parent, list):
        if leaf == "-":
            parent.append(value)
            return
        if not leaf.isdigit():
            raise ValueError(f"JSON Pointer array token must be an index or '-': {path}")
        index = int(leaf)
        if index > len(parent):
            raise ValueError(f"JSON Pointer array index is out of range: {path}")
        parent.insert(index, value)
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")

def _occurrence_value_matches(
    values: list[Any],
    expected: Any,
    occurrence: int | str,
) -> bool:
    if occurrence in {"all", "append"}:
        return bool(values) and (
            all(value == expected for value in values)
            if occurrence == "all"
            else values[-1] == expected
        )
    return occurrence < len(values) and values[occurrence] == expected
