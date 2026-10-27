"""THROWAWAY PROTOTYPE (GUR-212). Not for merge.

The revised CV wait loop: instead of one long `orchestrator.wait()`, sleep a
slice, inspect the container and fetch `/cv/progress.jsonl` with a short-timeout
client, report each new complete line, and do one final fetch after exit. A failed
or hung fetch is ignored and never changes the CV outcome.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from typing import TYPE_CHECKING, Any

import requests
import urllib3

if TYPE_CHECKING:
    from collections.abc import Callable

PROGRESS_PATH = "/cv/progress.jsonl"
PROGRESS_MAX_BYTES = 16 * 1024
SLICE_SECONDS = 2.0
SHORT_TIMEOUT_SECONDS = 10.0
ENGINE_LOST = (requests.RequestException, urllib3.exceptions.HTTPError)


def fetch_progress(api: Any, container_id: str) -> bytes:  # noqa: ANN401
    """Return the file's first PROGRESS_MAX_BYTES; b'' when absent."""
    from docker.errors import NotFound  # noqa: PLC0415

    try:
        stream, _stat = api.get_archive(container_id, PROGRESS_PATH)
    except NotFound:
        return b""
    archive = b"".join(stream)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.next()
        content = tar.extractfile(member) if member is not None else None
        return content.read(PROGRESS_MAX_BYTES) if content is not None else b""


class Follower:
    """Turn new complete lines past our own offset into reports."""

    def __init__(self, report: Callable[..., None], log: Callable[[str], None]) -> None:
        self.offset = 0
        self.report = report
        self.log = log

    def feed(self, content: bytes) -> None:
        end = content.rfind(b"\n", self.offset) + 1
        if end <= self.offset:
            if len(content) > self.offset:
                self.log(f"partial line pending ({len(content) - self.offset} B)")
            return
        for raw in content[self.offset : end].splitlines():
            self._line(raw)
        self.offset = end
        if len(content) > end:
            self.log(f"partial line pending ({len(content) - end} B)")

    def _line(self, raw: bytes) -> None:
        try:
            item = json.loads(raw)
        except ValueError:
            self.log("skipped an unparsable progress line")
            return
        if item.get("kind") == "setup":
            self.log(f"line: setup {item['status']}")
            self.report("mission-setup-done", setup_status=item["status"], s=int(item["s"]))
        elif item.get("kind") == "step":
            self.log(f"line: step {item['id']} {item['status']}")
            self.report(
                "mission-step",
                mission_step=item["id"],
                mission_status=item["status"],
                s=int(item.get("s", 0)),
            )


def follow(  # noqa: PLR0913
    *,
    short_api: Any,  # noqa: ANN401
    container_id: str,
    deadline: float,
    report: Callable[..., None],
    log: Callable[[str], None],
    fetch_api: Callable[[int], Any] | None = None,
) -> bool:
    """Wait for the orchestrator to exit; False on deadline or engine loss.

    fetch_api(poll_number) picks the client for a fetch (fault injection in the
    demo); by default short_api.
    """
    follower = Follower(report, log)
    poll = 0
    while True:
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(SLICE_SECONDS, max(0.0, deadline - time.monotonic())))
        poll += 1
        try:
            status = short_api.inspect_container(container_id)["State"]["Status"]
        except ENGINE_LOST:
            log("inspect failed: engine lost (handled exactly as today)")
            return False
        _fetch(follower, (fetch_api or (lambda _: short_api))(poll), container_id, log, poll)
        if status in {"exited", "dead"}:
            log(f"orchestrator {status}; final fetch")
            _fetch(follower, short_api, container_id, log, poll)
            return True


def _fetch(follower: Follower, api: Any, container_id: str, log: Callable[[str], None], poll: int) -> None:  # noqa: ANN401
    from docker.errors import DockerException  # noqa: PLC0415

    started = time.monotonic()
    try:
        content = fetch_progress(api, container_id)
    except (DockerException, *ENGINE_LOST) as error:
        log(f"poll {poll}: fetch failed after {time.monotonic() - started:.1f} s "
            f"({type(error).__name__}); ignored")
        return
    log(f"poll {poll}: fetched {len(content)} B")
    follower.feed(content)
