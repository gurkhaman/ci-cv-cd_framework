"""A local Responses endpoint for outcomes the real model cannot produce on demand.

Live tests cover generated, exhausted and rejected-key outcomes against the real
provider. A refusal, a content-filter stop, output outside the proposal schema
and a failed response cannot be requested from it, so these tests point a
generation config's base_url at this stub, which answers every request with one
recorded Responses body. The real SDK still parses every answer.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Generator

STUB_KEY = "stub-only-key"


def _response(
    status: str,
    content: list[dict[str, object]],
    **fields: object,
) -> dict[str, object]:
    return {
        "id": "resp_stub",
        "object": "response",
        "created_at": 0,
        "model": "stub-model",
        "status": status,
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "id": "msg_stub",
                "role": "assistant",
                "status": "completed",
                "content": content,
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
        **fields,
    }


def _text(text: str) -> list[dict[str, object]]:
    return [{"type": "output_text", "text": text, "annotations": []}]


# Recorded bodies by the generation outcome each one produces.
BODIES: dict[str, dict[str, object]] = {
    "content-filter": _response(
        "incomplete", [], incomplete_details={"reason": "content_filter"}
    ),
    "refusal": _response(
        "completed", [{"type": "refusal", "refusal": "Test-only refusal."}]
    ),
    "off-schema": _response("completed", _text('{"proposals": []}')),
    "failed": _response(
        "failed",
        [],
        error={"code": "server_error", "message": "Test-only failure."},
    ),
}


class _Handler(BaseHTTPRequestHandler):
    body: ClassVar[bytes] = b""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@contextmanager
def stub_provider(body: dict[str, object]) -> Generator[str]:
    """Serve one recorded body to every request; yield the base_url."""
    handler = type("Handler", (_Handler,), {"body": json.dumps(body).encode()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def stub_config(base_url: str) -> str:
    """Return a generation config that sends requests to the stub."""
    return (
        "schema_version: sdi.generation-config/v1\n"
        "model: stub-model\n"
        "reasoning_effort: low\n"
        "max_output_tokens: 1000\n"
        "timeout_seconds: 30\n"
        f"base_url: {base_url}\n"
        "api_key_env: VLLM_KEY\n"
    )
