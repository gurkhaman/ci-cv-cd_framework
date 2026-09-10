# Pipeline Integration Package

This independent `uv` project owns the supported `sdi-integration` CLI and the
deep `sdi_pipeline_integration` package boundary. The CLI, committed file
contracts, and the future language-neutral Stage-adapter process interface are
the supported interfaces. Internal Python modules are not compatibility
contracts.

Run all checks through `scripts/verify` rather than using an ambient Python
environment.
