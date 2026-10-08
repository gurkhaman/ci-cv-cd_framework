"""Public-boundary checks for assessing and ranking composition proposals."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
FAKE_DESCRIPTIONS = Path(__file__).parent / "data/service-descriptions"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"

NAVIGATION = ["nav2-navigation", "nav2-localization", "turtlebot3-bringup"]
SPEECH = ["whisper-audio-listener", "whisper-server"]
COMPLETE: dict[str, Any] = {
    "rationale": "Navigation and speech on their natural hosts.",
    "services": [
        {
            "service_id": service_id,
            "artifact_id": artifact_id,
            "host": host,
            "capability": capability,
        }
        for service_id, artifact_id, host, capability in [
            ("turtlebot3-bringup", "jazzy-debs", "waffle", "base and lidar"),
            ("nav2-localization", "jazzy-debs", "orin", "localization"),
            ("nav2-navigation", "jazzy-debs", "orin", "path planning"),
            ("v4l2-camera", "jazzy-debs", "waffle", "camera images"),
            ("face-recog", "jazzy-source", "orin", "face recognition"),
            ("whisper-audio-listener", "jazzy-source", "waffle", "audio capture"),
            ("whisper-server", "jazzy-cuda-source", "orin", "speech to text"),
        ]
    ],
    "coverage": [
        {
            "requirement_id": requirement_id,
            "status": status,
            "services": services,
            "reason": "Test-only claim.",
        }
        for requirement_id, status, services in [
            ("secure-book", "outside_composition", []),
            ("accept-spoken-request", "supported", SPEECH),
            ("resolve-rendezvous", "uncertain", ["whisper-server"]),
            ("navigate-to-rendezvous", "supported", NAVIGATION),
            ("verify-recipient-face", "supported", ["face-recog", "v4l2-camera"]),
            ("transfer-book", "outside_composition", []),
            ("acknowledge-receipt", "supported", SPEECH),
            ("return-to-origin", "supported", NAVIGATION),
        ]
    ],
}


def _variant(
    *,
    drop: tuple[str, ...] = (),
    add: tuple[dict[str, str], ...] = (),
    hosts: dict[str, str | None] | None = None,
    coverage: dict[str, tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    proposal = copy.deepcopy(COMPLETE)
    proposal["services"] = [
        service for service in proposal["services"] if service["service_id"] not in drop
    ] + [{"capability": "test-only", **service} for service in add]
    for service in proposal["services"]:
        if service["service_id"] in (hosts or {}):
            host = (hosts or {})[service["service_id"]]
            if host is None:
                del service["host"]
            else:
                service["host"] = host
    for entry in proposal["coverage"]:
        if entry["requirement_id"] in (coverage or {}):
            entry["status"], entry["services"] = (coverage or {})[
                entry["requirement_id"]
            ]
    return proposal


def _assess(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    profile_edit: tuple[str, str] = ("", ""),
) -> subprocess.CompletedProcess[str]:
    services = tmp_path / "services"
    if not services.exists():
        shutil.copytree(SERVICE_REPOSITORY, services)
        shutil.copytree(FAKE_DESCRIPTIONS, services / "fakes")
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE.read_text().replace(*profile_edit))
    proposals_path = tmp_path / "proposals.json"
    proposals_path.write_text(json.dumps({"proposals": proposals}))
    return subprocess.run(
        [
            "sdi-integration",
            "assess-proposals",
            "--service-repository",
            str(services),
            "--target-profile",
            str(profile),
            "--requirements-specification",
            str(REQUIREMENTS),
            "--proposals",
            str(proposals_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _assessment(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    profile_edit: tuple[str, str] = ("", ""),
) -> dict[str, Any]:
    completed = _assess(tmp_path, proposals, profile_edit)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _kinds(proposal: dict[str, Any]) -> list[str]:
    return sorted({finding["kind"] for finding in proposal["findings"]})


WITHOUT_FACE_RECOG = _variant(
    drop=("face-recog",), coverage={"verify-recipient-face": ("missing", [])}
)


@pytest.mark.parametrize(
    ("proposals", "scores", "preferred", "tie"),
    [
        pytest.param(
            [
                _variant(
                    drop=("nav2-localization",),
                    add=(
                        {
                            "service_id": "fake-unmet-dependency",
                            "artifact_id": "jazzy-source",
                            "host": "orin",
                        },
                    ),
                ),
                COMPLETE,
            ],
            [(2, 2), (0, 0)],
            "proposal-2",
            False,
            id="complete-beats-without-localization",
        ),
        pytest.param(
            [WITHOUT_FACE_RECOG, COMPLETE],
            [(0, 1), (0, 0)],
            "proposal-2",
            False,
            id="missing-count-decides",
        ),
        pytest.param(
            [
                _variant(
                    drop=("v4l2-camera",),
                    coverage={"verify-recipient-face": ("supported", ["face-recog"])},
                ),
                WITHOUT_FACE_RECOG,
            ],
            [(1, 0), (0, 1)],
            "proposal-2",
            False,
            id="gaps-beat-missing",
        ),
        pytest.param(
            [COMPLETE, _variant(hosts={"nav2-navigation": "waffle"})],
            [(0, 0), (0, 0)],
            "proposal-1",
            True,
            id="tie-broken-by-model-order",
        ),
    ],
)
def test_prefers_the_proposal_with_fewest_gaps_then_missing_coverage(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    scores: list[tuple[int, int] | None],
    preferred: str,
    *,
    tie: bool,
) -> None:
    assessment = _assessment(tmp_path, proposals)

    assert [
        None
        if proposal["score"] is None
        else (proposal["score"]["gaps"], proposal["score"]["missing"])
        for proposal in assessment["proposals"]
    ] == scores
    assert assessment["preferred"] == preferred
    assert assessment["tie_broken_by_model_order"] is tie
    assert assessment["proposals"][0]["proposal"] == proposals[0]


@pytest.mark.parametrize(
    ("proposal", "profile_edit", "kinds", "score", "placements"),
    [
        pytest.param(
            _variant(
                drop=("v4l2-camera",),
                add=(
                    {
                        "service_id": "fake-compressed-camera",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                ),
                coverage={
                    "verify-recipient-face": (
                        "supported",
                        ["face-recog", "fake-compressed-camera"],
                    )
                },
            ),
            ("", ""),
            ["binding-rejected", "missing-provider"],
            (1, 0),
            {},
            id="type-mismatch",
        ),
        pytest.param(
            _variant(
                drop=("v4l2-camera",),
                add=(
                    {
                        "service_id": "fake-best-effort-camera",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                ),
                coverage={
                    "verify-recipient-face": (
                        "supported",
                        ["face-recog", "fake-best-effort-camera"],
                    )
                },
            ),
            ("", ""),
            ["binding-rejected", "missing-provider"],
            (1, 0),
            {},
            id="qos-mismatch",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "fake-unverified-endpoints",
                        "artifact_id": "jazzy-source",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["endpoints-unverified"],
            (0, 0),
            {},
            id="unverified-endpoints",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "hri-face-detect",
                        "artifact_id": "humble-source",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["placement-rejected"],
            (0, 0),
            {"hri-face-detect": "rejected"},
            id="baseline-mismatch",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "fake-missing-artifact",
                        "artifact_id": "invented",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["missing-artifact"],
            (1, 0),
            {"fake-missing-artifact": "unresolved"},
            id="artifact-id-without-artifacts",
        ),
        pytest.param(
            {
                **_variant(hosts={"whisper-server": "waffle"}),
                "services": [
                    *_variant(drop=("whisper-server",))["services"],
                    {
                        "service_id": "whisper-server",
                        "host": "waffle",
                        "capability": "speech to text",
                    },
                ],
            },
            ("", ""),
            ["coverage-downgraded", "placement-rejected"],
            (0, 3),
            {"whisper-server": "rejected"},
            id="removed-service-keeps-no-gaps",
        ),
        pytest.param(
            COMPLETE,
            ("    devices: [opencr, lidar, camera, microphone]\n", ""),
            ["unresolved-placement"],
            (3, 0),
            dict.fromkeys(
                ["turtlebot3-bringup", "v4l2-camera", "whisper-audio-listener"],
                "unresolved",
            ),
            id="host-without-device-list",
        ),
        pytest.param(
            _variant(hosts={"face-recog": "jetson-nano", "v4l2-camera": None}),
            ("", ""),
            ["unresolved-placement"],
            (2, 0),
            dict.fromkeys(["face-recog", "v4l2-camera"], "unresolved"),
            id="unknown-and-omitted-host",
        ),
        pytest.param(
            COMPLETE,
            ("connections:\n  - [waffle, orin]\n", ""),
            ["unresolved-connection"],
            (1, 0),
            {},
            id="no-connection",
        ),
    ],
)
def test_records_findings_and_their_gaps(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    proposal: dict[str, Any],
    profile_edit: tuple[str, str],
    kinds: list[str],
    score: tuple[int, int],
    placements: dict[str, str],
) -> None:
    assessment = _assessment(tmp_path, [proposal], profile_edit)

    [assessed] = assessment["proposals"]
    assert _kinds(assessed) == kinds
    assert (assessed["score"]["gaps"], assessed["score"]["missing"]) == score
    assert {
        service["service_id"]: service["placement"]
        for service in assessed["services"]
        if service["placement"] != "resolved"
    } == placements
    assert assessment["preferred"] == "proposal-1"


@pytest.mark.parametrize(
    "proposal",
    [
        pytest.param(
            _variant(add=({"service_id": "invented", "host": "orin"},)),
            id="unknown-service",
        ),
        pytest.param(
            _variant(
                drop=("face-recog",),
                add=(
                    {
                        "service_id": "face-recog",
                        "artifact_id": "invented",
                        "host": "orin",
                    },
                ),
            ),
            id="unknown-artifact",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "face-recog",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                )
            ),
            id="duplicate-service",
        ),
        pytest.param(
            {**COMPLETE, "coverage": COMPLETE["coverage"][1:]},
            id="coverage-mismatch",
        ),
        pytest.param(
            _variant(coverage={"transfer-book": ("missing", ["face-recog"])}),
            id="negative-status-cites-services",
        ),
        pytest.param(
            {
                **COMPLETE,
                "services": [
                    {
                        "service_id": "whisper-server",
                        "artifact_id": "jazzy-cuda-source",
                        "host": "waffle",
                        "capability": "speech to text",
                    }
                ],
            },
            id="no-service-remains",
        ),
    ],
)
def test_reports_no_preferred_proposal_when_none_is_eligible(
    tmp_path: Path, proposal: dict[str, Any]
) -> None:
    assessment = _assessment(tmp_path, [proposal])

    [assessed] = assessment["proposals"]
    assert assessed["eligible"] is False
    assert assessed["score"] is None
    assert "ineligible" in _kinds(assessed)
    assert assessment["preferred"] is None
    assert assessment["reason"] == "no proposal is eligible"


def test_rejects_proposals_outside_the_schema(tmp_path: Path) -> None:
    completed = _assess(tmp_path, [])

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "proposals.json: contract validation failed" in completed.stderr
