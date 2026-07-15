from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlsplit

SSE_EVENTS: Final[tuple[bytes, ...]] = (
    b'event: chunk\ndata: {"sequence":1,"value":"alpha"}\n\n',
    b'event: chunk\ndata: {"sequence":2,"value":"beta"}\n\n',
    b'event: complete\ndata: fixture-complete\n\n',
)


class ToolchainValidationHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fixture_root: Path
    slow_started_event: threading.Event
    stream_state_lock: threading.Lock
    stream_states: dict[str, dict[str, object]]

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send_file(
                self.fixture_root / "index.html",
                "text/html; charset=utf-8",
                extra_headers={
                    "Set-Cookie": (
                        "fixture_session=fixture-session; Path=/; HttpOnly; SameSite=Lax"
                    )
                },
            )
            return
        if path == "/app.js":
            self._send_file(self.fixture_root / "app.js", "text/javascript; charset=utf-8")
            return
        if path == "/api/sse":
            self._send_sse()
            return
        if path == "/slow":
            self.slow_started_event.set()
            query = parse_qs(urlsplit(self.path).query)
            try:
                seconds = min(15.0, max(0.0, float(query.get("seconds", ["10"])[0])))
            except ValueError:
                seconds = 10.0
            time.sleep(seconds)
            body = b"<!doctype html><title>slow navigation completed</title>"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if path == "/health":
            self._send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/stateful-stream":
            self._handle_stateful_stream()
            return
        if path != "/api/echo":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            request_payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "invalid-json"}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json(
            {
                "ok": True,
                "marker": "stage0-response",
                "received": request_payload,
            }
        )

    def _handle_stateful_stream(self) -> None:
        cookie = self.headers.get("Cookie", "")
        authorization = self.headers.get("Authorization", "")
        if (
            "fixture_session=fixture-session" not in cookie
            and authorization != "Bearer fixture-token"
        ):
            self._send_json(
                {"ok": False, "error": "authentication-required"},
                HTTPStatus.UNAUTHORIZED,
            )
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                {"ok": False, "error": "invalid-json"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if isinstance(payload, dict) and payload.get("profile") == "fixture-server-error":
            self._send_json(
                {"ok": False, "error": "synthetic-server-failure"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        records = payload.get("records") if isinstance(payload, dict) else None
        record = (
            records[0]
            if isinstance(records, list) and records and isinstance(records[0], dict)
            else None
        )
        source = record.get("source") if isinstance(record, dict) else None
        content = record.get("content") if isinstance(record, dict) else None
        segments = content.get("segments") if isinstance(content, dict) else None
        required = {
            "job_id": payload.get("job_id")
            if isinstance(payload, dict)
            else None,
            "profile": payload.get("profile") if isinstance(payload, dict) else None,
            "records[0].record_id": (
                record.get("record_id") if isinstance(record, dict) else None
            ),
            "records[0].source.kind": (
                source.get("kind") if isinstance(source, dict) else None
            ),
            "records[0].content.segments[0]": (
                segments[0] if isinstance(segments, list) and segments else None
            ),
            "cursor_id": payload.get("cursor_id")
            if isinstance(payload, dict)
            else None,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            self._send_json(
                {
                    "ok": False,
                    "error": "missing-required-fields",
                    "missing": missing,
                },
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        job_id = str(payload["job_id"])
        client_record_id = str(record["record_id"])
        cursor_id = str(payload["cursor_id"])
        server_record_id = f"server-{client_record_id}"
        with self.stream_state_lock:
            state = self.stream_states.setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "records": {},
                    "current_cursor": cursor_id,
                },
            )
            stored_records = state["records"]
            assert isinstance(stored_records, dict)
            if client_record_id in stored_records:
                self._send_json(
                    {
                        "ok": False,
                        "error": "duplicate-record-id",
                        "field": "records[0].record_id",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            stored_records[client_record_id] = {
                "record_id": client_record_id,
                "cursor_id": cursor_id,
                "kind": source["kind"],
                "segments": segments,
            }
            stored_records[server_record_id] = {
                "record_id": server_record_id,
                "cursor_id": client_record_id,
                "kind": "server",
                "segments": ["fixture result"],
            }
            state["current_cursor"] = server_record_id
            state_snapshot = json.loads(json.dumps(state))
        output_events = (
            {
                "type": "record_open",
                "job_id": job_id,
                "record": {
                    "record_id": server_record_id,
                    "cursor_id": client_record_id,
                    "source": {"kind": "server"},
                },
            },
            {
                "type": "record_delta",
                "record_id": server_record_id,
                "delta": "fixture result contains custom terminal text",
            },
            {
                "type": "state_snapshot",
                "job_id": job_id,
                "current_cursor": server_record_id,
                "records": state_snapshot["records"],
                "profile": payload["profile"],
                "optional_timezone_seen": "timezone_offset_min" in payload,
                "tracking_seen": "tracking_id" in payload,
            },
        )
        self._send_sse_json(output_events)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_file(
        self,
        path: Path,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in SSE_EVENTS:
            self.wfile.write(event)
            self.wfile.flush()
            time.sleep(0.05)
        self.close_connection = True

    def _send_sse_json(self, events: tuple[dict[str, object], ...]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for index, payload in enumerate(events):
            event = (
                f"event: chunk\nid: {index}\ndata: "
                f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n"
            ).encode()
            self.wfile.write(event)
            self.wfile.flush()
            time.sleep(0.03)
        self.wfile.write(b"event: complete\ndata: fixture-complete\n\n")
        self.wfile.flush()
        self.close_connection = True


def start_server(
    fixture_root: Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    handler_type = type(
        "BoundToolchainValidationHandler",
        (ToolchainValidationHandler,),
        {
            "fixture_root": fixture_root,
            "slow_started_event": threading.Event(),
            "stream_state_lock": threading.Lock(),
            "stream_states": {},
        },
    )
    server = ThreadingHTTPServer((host, port), handler_type)
    thread = threading.Thread(target=server.serve_forever, name="stage0-fixture", daemon=True)
    thread.start()
    return server, thread
