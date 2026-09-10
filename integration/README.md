# Pipeline Integration Package

This independent `uv` project owns the supported `sdi-integration` CLI and the
deep `sdi_pipeline_integration` package boundary. The CLI, committed file
contracts, and the future language-neutral Stage-adapter process interface are
the supported interfaces. Internal Python modules are not compatibility
contracts.

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
