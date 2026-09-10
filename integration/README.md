# Pipeline Integration Package

This independent `uv` project owns the supported `sdi-integration` CLI and the
deep `sdi_pipeline_integration` package boundary. The CLI, committed file
contracts, and the language-neutral Stage-adapter process interface are the
supported interfaces. Internal Python modules are not compatibility contracts.

Run all checks through `scripts/verify` rather than using an ambient Python
environment.

## Identify A Committed Run

`identify-run` reads the request and every referenced input directly from one
resolved Git commit. It accepts only the protected-main dispatch ref and never
reads dirty or staged worktree content:

```sh
uv run --project integration sdi-integration identify-run \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml
```

The repository must have one uncredentialed GitHub `origin` URL. Successful
validation emits one JSON record containing a newly assigned lowercase UUIDv4
Execution ID and exact-byte provenance. Validation failures exit with status 2,
write no record, and assign no Execution ID.

## Execute One Stage

`execute-stage` identifies the committed run, selects its reviewed adapter
descriptor, verifies the descriptor-bound Stage profile, and invokes the adapter
through the one-shot process interface. It accepts declared files only after the
complete candidate bundle, response correlation, file limits, and Domain schemas
validate:

```sh
uv run --project integration sdi-integration execute-stage \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml \
  --descriptor-path deployment/jenkins/adapters/composition-fixture-v1.yaml \
  --attempt-root artifacts/composition-attempt
```

The adapter process exposes only:

```sh
sdi-fixture-adapter run \
  --request request.json \
  --input-root inputs \
  --output-root candidate
```

The accepted envelope records `execution_conclusion: succeeded`,
`implementation_mode: fixture`, and `domain_outcome: not_evaluated` separately.
The deterministic blueprint and deployment schema prove pipeline-interface
handling only. They are not composition, deployment, Validation, or KPI evidence.

## Dispatch The Four-Stage Fixture

`dispatch-local` assigns one Execution ID, executes the reviewed composition,
image-build, CV, and CD descriptors sequentially, and passes only exact accepted
files along the least-required graph. It atomically publishes a complete archive
candidate only after all attempts, Domain outputs, diagnostics, result metadata,
and checksums validate:

```sh
uv run --project integration sdi-integration dispatch-local \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml \
  --bundle-root artifacts/s-04-tc-03-c-01
```

The archive contains the fixed `pipeline-integration-result.json`, five small
Domain handoff files, and four bounded diagnostics. The result is the manifest;
its checksummed inventory covers every other archive file without a recursive
self-digest. Revalidate the complete archive independently with:

```sh
uv run --project integration sdi-integration validate-bundle \
  --bundle-root artifacts/s-04-tc-03-c-01
```

Every attempt remains `implementation_mode: fixture` and
`domain_outcome: not_evaluated`; the result explicitly records
`kpi_evaluation: not_evaluated` and contains no overall Domain or six-combination
verdict.
