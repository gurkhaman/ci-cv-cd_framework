"""THROWAWAY PROTOTYPE (GUR-212): run one Image build attempt with a scripted adapter.

Runs the real (patched) runtime in this process under the CLI's own SIGINT/SIGTERM
handler, so cancellation behaves as in `execute-jenkins-stage`. The Image build
inputs are synthetic: a Fixture composition run supplies valid documents, and the
deployment schema is rewritten to N placements svc-01..svc-NN.

usage: gur212_prototype_runner.py WORKDIR ADAPTER WORK_LIMIT_SECONDS [SERVICES]
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdi_pipeline_integration._cli import _external_cancellation  # noqa: E402
from sdi_pipeline_integration._git_input import GitRepository  # noqa: E402
from sdi_pipeline_integration._run_input import identify_committed_run  # noqa: E402
from sdi_pipeline_integration._stage_registry import STAGES  # noqa: E402
from sdi_pipeline_integration._stage_runtime import (  # noqa: E402
    ExternalCancellation,
    SuppliedInputs,
    execute_identified_stage,
    stage_inputs,
)
from tests._fixture_inputs import (  # noqa: E402
    COMPOSITION_DESCRIPTOR,
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    RUN_REQUEST_PATH,
    commit_fixture_repository,
    run_git,
)


def main() -> int:
    workdir = Path(sys.argv[1])
    adapter = Path(sys.argv[2]).resolve()
    work_limit = int(sys.argv[3])
    services = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    workdir.mkdir(parents=True, exist_ok=True)
    repository, _ = commit_fixture_repository(workdir)
    descriptor_path = STAGES["image_build"].jenkins_descriptor
    descriptor = repository / descriptor_path
    descriptor.write_text(
        descriptor.read_text().replace(
            "entrypoint: sdi-image-build-adapter", f"entrypoint: {adapter}"
        )
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Select scripted Image build adapter")
    commit = run_git(repository, "rev-parse", "HEAD")
    git = GitRepository(repository, commit)
    identified = identify_committed_run(
        repository_path=repository,
        requested_ref="refs/heads/main",
        resolved_commit=commit,
        run_request_path=RUN_REQUEST_PATH,
    )
    with contextlib.redirect_stderr(io.StringIO()):  # keep setup lines off the proof
        composition = execute_identified_stage(
            repository=git,
            identified=identified,
            descriptor_path=COMPOSITION_DESCRIPTOR,
            attempt_root=workdir / "composition",
            available_inputs=stage_inputs(
                git,
                identified,
                SuppliedInputs(
                    service_repository=FIXTURE_SERVICE_REPOSITORY,
                    composition_config=FIXTURE_COMPOSITION_CONFIG,
                ),
                stage="composition",
            ),
        )
    real = {
        slot: dataclasses.replace(
            source,
            producer_implementation_mode="implemented",
            producer_domain_outcome="succeeded",
        )
        for slot, source in composition.outputs.items()
    }
    schema = json.loads(real["deployment_schema"].content)
    schema["placements"] = [
        {"service_id": f"svc-{k:02d}", "status": "resolved", "artifact_id": "a", "host": "orin"}
        for k in range(1, services + 1)
    ]
    real["deployment_schema"] = dataclasses.replace(
        real["deployment_schema"], content=json.dumps(schema).encode()
    )
    inputs = stage_inputs(
        git,
        identified,
        SuppliedInputs(service_repository=FIXTURE_SERVICE_REPOSITORY),
        stage="image_build",
    ) | real
    attempt_root = workdir / "attempt"
    try:
        with _external_cancellation():
            execution = execute_identified_stage(
                repository=git,
                identified=identified,
                descriptor_path=descriptor_path,
                attempt_root=attempt_root,
                available_inputs=inputs,
                work_limit_seconds=work_limit,
            )
    except ExternalCancellation as cancellation:
        shutil.rmtree(attempt_root, ignore_errors=True)
        return 128 + cancellation.signal_number
    envelope = execution.envelope
    sys.stdout.write(
        json.dumps(
            {
                "execution_conclusion": envelope.execution_conclusion,
                "reason": None if envelope.reason is None else envelope.reason.code,
                "process": envelope.process.model_dump(mode="json") if envelope.process else None,
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
