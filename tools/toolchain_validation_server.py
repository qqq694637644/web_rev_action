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
    b'event: message\ndata: {"sequence":1,"value":"alpha"}\n\n',
    b'event: message\ndata: {"sequence":2,"value":"beta"}\n\n',
    b'event: done\ndata: [DONE]\n\n',
)


class ToolchainValidationHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fixture_root: Path
    slow_started_event: threading.Event
    conversation_lock: threading.Lock
    conversation_states: dict[str, dict[str, object]]

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
                        "pandora_session=fixture-session; Path=/; HttpOnly; SameSite=Lax"
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
        if path == "/api/pandora/conversation":
            self._handle_pandora_conversation()
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

    def _handle_pandora_conversation(self) -> None:
        cookie = self.headers.get("Cookie", "")
        authorization = self.headers.get("Authorization", "")
        if (
            "pandora_session=fixture-session" not in cookie
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
        messages = payload.get("messages") if isinstance(payload, dict) else None
        message = (
            messages[0]
            if isinstance(messages, list) and messages and isinstance(messages[0], dict)
            else None
        )
        author = message.get("author") if isinstance(message, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        required = {
            "conversation_id": payload.get("conversation_id")
            if isinstance(payload, dict)
            else None,
            "model": payload.get("model") if isinstance(payload, dict) else None,
            "messages[0].id": message.get("id") if isinstance(message, dict) else None,
            "messages[0].author.role": (
                author.get("role") if isinstance(author, dict) else None
            ),
            "messages[0].content.parts[0]": (
                parts[0] if isinstance(parts, list) and parts else None
            ),
            "parent_message_id": payload.get("parent_message_id")
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
        conversation_id = str(payload["conversation_id"])
        user_message_id = str(message["id"])
        parent_message_id = str(payload["parent_message_id"])
        assistant_message_id = f"assistant-{user_message_id}"
        with self.conversation_lock:
            state = self.conversation_states.setdefault(
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "mapping": {},
                    "current_node": parent_message_id,
                },
            )
            mapping = state["mapping"]
            assert isinstance(mapping, dict)
            if user_message_id in mapping:
                self._send_json(
                    {
                        "ok": False,
                        "error": "duplicate-message-id",
                        "field": "messages[0].id",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            mapping[user_message_id] = {
                "id": user_message_id,
                "parent": parent_message_id,
                "role": author["role"],
                "content": parts,
            }
            mapping[assistant_message_id] = {
                "id": assistant_message_id,
                "parent": user_message_id,
                "role": "assistant",
                "content": ["fixture answer"],
            }
            state["current_node"] = assistant_message_id
            state_snapshot = json.loads(json.dumps(state))
        events = (
            {
                "type": "message_start",
                "conversation_id": conversation_id,
                "message": {
                    "id": assistant_message_id,
                    "parent_id": user_message_id,
                    "author": {"role": "assistant"},
                },
            },
            {
                "type": "message_delta",
                "message_id": assistant_message_id,
                "delta": "fixture answer",
            },
            {
                "type": "conversation_state",
                "conversation_id": conversation_id,
                "current_node": assistant_message_id,
                "mapping": state_snapshot["mapping"],
                "model": payload["model"],
                "optional_timezone_seen": "timezone_offset_min" in payload,
                "tracking_seen": "tracking_id" in payload,
            },
        )
        self._send_sse_json(events)

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
                f"event: message\nid: {index}\ndata: "
                f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n"
            ).encode()
            self.wfile.write(event)
            self.wfile.flush()
            time.sleep(0.03)
        self.wfile.write(b"event: done\ndata: [DONE]\n\n")
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
            "conversation_lock": threading.Lock(),
            "conversation_states": {},
        },
    )
    server = ThreadingHTTPServer((host, port), handler_type)
    thread = threading.Thread(target=server.serve_forever, name="stage0-fixture", daemon=True)
    thread.start()
    return server, thread
