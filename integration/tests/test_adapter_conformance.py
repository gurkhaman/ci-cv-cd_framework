"""Every adapter refuses a bad request before it publishes anything."""

from __future__ import annotations

import hashlib
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._adapter_kit import MAX_REQUEST_BYTES, DomainAdapter
from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._stage_contracts import AdapterDescriptor, StageProfile
from sdi_pipeline_integration._stage_profiles import stage_profile_bytes
from sdi_pipeline_integration._stage_registry import STAGE_ORDER
from sdi_pipeline_integration._yaml_input import parse_yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sdi_pipeline_integration._stage_registry import StageName

REPOSITORY_ROOT = Path(__file__).parents[2]
ADAPTER_SCRIPTS = sorted(
    {
        AdapterDescriptor.model_validate(
            parse_yaml(path.read_bytes(), str(path))
        ).entrypoint
        for path in (REPOSITORY_ROOT / "deployment/jenkins/adapters").glob("*.yaml")
    }
)

type Main = Callable[[Sequence[str] | None], int]


def _main(script: str) -> Main:
    (entry_point,) = entry_points(group="console_scripts", name=script)
    return entry_point.load()


def _request(stage: StageName) -> bytes:
    profile = StageProfile.model_validate(
        parse_yaml(stage_profile_bytes(stage), f"{stage} profile")
    )
    content = b"x"
    return canonical_json(
        {
            "schema_version": "sdi.stage-adapter-request/v1",
            "correlation": {
                "execution_id": "0b6f5d1e-2c3a-4d5e-8f60-718293a4b5c6",
                "stage": stage,
                "attempt_number": 1,
            },
            "work_limit_seconds": profile.work_limit_seconds,
            "inputs": [
                grant.model_dump(mode="json")
                | {
                    "byte_size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for grant in profile.inputs
            ],
            "outputs": [item.model_dump(mode="json") for item in profile.outputs],
            "diagnostic": profile.diagnostic.model_dump(mode="json"),
        }
    )


def _refuses(
    script: str,
    request: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request)
    (tmp_path / "inputs").mkdir()
    output_root = tmp_path / "candidate"
    output_root.mkdir()

    exit_code = _main(script)(
        [
            "run",
            "--request",
            str(request_path),
            "--input-root",
            str(tmp_path / "inputs"),
            "--output-root",
            str(output_root),
        ]
    )

    assert exit_code == 2, f"{script} accepted the request"
    assert list(output_root.iterdir()) == [], f"{script} published before refusing"
    assert capsys.readouterr().err.startswith(f"{script}: ")


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_an_oversized_request_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request("cd")
    padded = request[:-1] + b" " * (MAX_REQUEST_BYTES + 1 - len(request)) + b"}"
    _refuses(script, padded, tmp_path, capsys)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_malformed_request_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuses(script, b"{malformed", tmp_path, capsys)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_request_for_another_stage_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = _main(script)
    if not isinstance(main, DomainAdapter):
        pytest.skip("the Fixture adapter replays a case for every Stage")
    other = STAGE_ORDER[(STAGE_ORDER.index(main.stage) + 1) % len(STAGE_ORDER)]
    _refuses(script, _request(other), tmp_path, capsys)
