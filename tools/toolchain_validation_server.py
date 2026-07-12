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

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send_file(self.fixture_root / "index.html", "text/html; charset=utf-8")
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

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        },
    )
    server = ThreadingHTTPServer((host, port), handler_type)
    thread = threading.Thread(target=server.serve_forever, name="stage0-fixture", daemon=True)
    thread.start()
    return server, thread
