from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.toolchain_validation_server import start_server


def request(
    url: str,
    *,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    outgoing = Request(url, data=body, headers=request_headers, method="POST" if body else "GET")
    try:
        with urlopen(outgoing, timeout=5) as response:  # noqa: S310
            return response.status, dict(response.headers.items()), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def valid_payload(*, profile: str = "fixture-profile") -> dict:
    return {
        "job_id": "synthetic-job",
        "profile": profile,
        "records": [
            {
                "record_id": "record-1",
                "source": {"kind": "client"},
                "content": {"format": "text", "segments": ["fixture input"]},
            }
        ],
        "cursor_id": "root-cursor",
        "tracking_id": "optional-tracking",
    }


def test_authenticated_stateful_stream_fixture_records_2xx_4xx_and_5xx() -> None:
    fixture_root = Path("tests/fixtures/toolchain_validation")
    server, thread = start_server(fixture_root)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page_status, page_headers, _ = request(f"{base_url}/")
        cookie = page_headers["Set-Cookie"].split(";", 1)[0]
        assert page_status == 200

        for attempt in range(25):
            unauthorized_status, _, unauthorized_body = request(
                f"{base_url}/api/stateful-stream",
                payload={
                    **valid_payload(),
                    "job_id": f"unauthorized-job-{attempt}",
                },
            )
            assert unauthorized_status == 401
            assert json.loads(unauthorized_body)["error"] == "authentication-required"

        invalid_status, _, invalid_body = request(
            f"{base_url}/api/stateful-stream",
            payload={"job_id": "synthetic-job"},
            headers={"Cookie": cookie},
        )
        assert invalid_status == 422
        assert json.loads(invalid_body)["error"] == "missing-required-fields"

        success_status, success_headers, success_body = request(
            f"{base_url}/api/stateful-stream",
            payload=valid_payload(),
            headers={"Cookie": cookie},
        )
        assert success_status == 200
        assert success_headers["Content-Type"].startswith("text/event-stream")
        assert b"state_snapshot" in success_body
        assert b"fixture-complete" in success_body

        duplicate_status, _, duplicate_body = request(
            f"{base_url}/api/stateful-stream",
            payload=valid_payload(),
            headers={"Cookie": cookie},
        )
        assert duplicate_status == 409
        assert json.loads(duplicate_body)["error"] == "duplicate-record-id"

        failure_status, _, failure_body = request(
            f"{base_url}/api/stateful-stream",
            payload=valid_payload(profile="fixture-server-error"),
            headers={"Authorization": "Bearer fixture-token"},
        )
        assert failure_status == 500
        assert json.loads(failure_body)["error"] == "synthetic-server-failure"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
