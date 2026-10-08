"""Public-boundary checks for model-generated composition proposals."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"
API_KEY = "sk-test-only-secret"

REQUIREMENT_IDS = [
    "secure-book",
    "accept-spoken-request",
    "resolve-rendezvous",
    "navigate-to-rendezvous",
    "verify-recipient-face",
    "transfer-book",
    "acknowledge-receipt",
    "return-to-origin",
]


def _proposals(service_id: str) -> dict[str, Any]:
    return {
        "proposals": [
            {
                "rationale": "Test-only controlled response.",
                "services": [
                    {
                        "service_id": service_id,
                        "artifact_id": "jazzy-debs",
                        "host": "orin",
                        "capability": "path planning",
                    }
                ],
                "coverage": [
                    {
                        "requirement_id": requirement_id,
                        "status": "missing",
                        "services": [],
                        "reason": "Test-only claim.",
                    }
                    for requirement_id in REQUIREMENT_IDS
                ],
            }
        ]
    }


def _response(proposals: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": json.dumps(proposals),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 30,
        },
    }


@contextmanager
def _fake_openai(status: int, body: dict[str, Any]) -> Generator[str, None, None]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            _ = self.wfile.write(payload)

        def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
            del format, arguments

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _generate(
    tmp_path: Path, status: int, body: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    with _fake_openai(status, body) as base_url:
        return subprocess.run(
            [
                "sdi-integration",
                "generate-proposals",
                "--service-repository",
                str(SERVICE_REPOSITORY),
                "--target-profile",
                str(PROFILE),
                "--requirements-specification",
                str(REQUIREMENTS),
                "--output",
                str(tmp_path / "proposals.json"),
                "--evidence",
                str(tmp_path / "evidence.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "OPENAI_API_KEY": API_KEY,
                "OPENAI_BASE_URL": base_url,
                "NO_PROXY": "127.0.0.1",
                "no_proxy": "127.0.0.1",
            },
        )


def test_generates_proposals_and_records_evidence(tmp_path: Path) -> None:
    expected = _proposals("nav2-navigation")
    completed = _generate(tmp_path, 200, _response(expected))

    assert completed.returncode == 0, completed.stderr
    proposals = json.loads((tmp_path / "proposals.json").read_text())
    assert proposals == expected
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "generated"
    assert evidence["request"]["model"] == "gpt-6-luna"
    assert "nav2-navigation" in evidence["request"]["input"]
    assert evidence["request"]["text"]["format"]["strict"] is True
    assert evidence["response"]["id"] == "resp_test"
    assert API_KEY not in evidence_text + completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        pytest.param(
            200, _response(_proposals("invented-service")), "invalid", id="invalid"
        ),
        pytest.param(
            200,
            _response(_proposals("nav2-navigation"))
            | {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "exhausted",
            id="exhausted",
        ),
        pytest.param(
            500,
            {"error": {"message": "test-only failure"}},
            "provider_error",
            id="provider",
        ),
    ],
)
def test_generation_failure_writes_no_proposals(
    tmp_path: Path, status: int, body: dict[str, Any], outcome: str
) -> None:
    completed = _generate(tmp_path, status, body)

    assert completed.returncode == 1, completed.stderr
    assert outcome in completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["outcome"] == outcome
