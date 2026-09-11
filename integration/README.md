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

## Jenkins Pipeline Operations

The root `Jenkinsfile` composes five public CLI operations rather than parsing or
constructing contracts in Groovy:

- `preflight-jenkins-run` preserves the submitted Execution ID and revalidates
  the seven scalar handoff values, committed inputs, all descriptors and Stage
  profiles, settled default limits, and the allocated resource mapping.
- `execute-jenkins-stage` reconstructs prior accepted attempts from bounded
  Jenkins stashes, validates every transferred byte and the trusted deployment
  work limit, and executes only the next reviewed descriptor on its matching
  immutable agent. An expired run deadline returns control without launching an
  adapter so integration-owned finalization can create the typed skip.
- `attempt-allows-continuation` derives whether another Domain Stage may run.
- `finalize-jenkins-run` creates typed downstream skips, assembles the exact
  archive, and validates it on the integration agent.
- `bundle-conclusion` revalidates the bundle and derives Jenkins machinery
  success without converting a valid negative Domain outcome into machinery
  failure.

These commands are pipeline-facing primitives. Operators submit only through the
fixed Jenkins job; they do not manually coordinate the primitives or reuse their
temporary directories as result authority.

## Settled Failure Semantics

`dispatch-local` publishes a complete contract-valid bundle whenever finalization
remains possible. Handled failures, crashes, deadline expiry, rejected candidate
transactions, unavailable implementations, and blocked dependencies appear as
typed attempts; skipped attempts never imply that a process ran. Adapter or
deadline failures return status 1 after printing the result. A valid negative
Domain outcome still returns status 0 because adapter execution succeeded.

Candidate acceptance is transactional. A malformed, missing, changing, unsafe,
undeclared, oversized, identity-mismatched, or schema-invalid candidate
contributes no adapter-authored files. The resulting failed attempt explicitly
records that no adapter response was accepted. An implemented adapter cannot
consume unevaluated Fixture output, while an implemented output may feed a later
Fixture during incremental replacement.

External `SIGINT` or `SIGTERM` cancellation is the deliberate exception: the CLI
immediately terminates the active adapter process tree, removes attempt-local and
bundle staging state, and publishes neither Stage attempts nor a Pipeline
integration result.
