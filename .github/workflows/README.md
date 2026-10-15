# Pipeline Integration Workflow

`pipeline-integration.yml` is the sole external Pipeline integration trigger. It
accepts one committed run-request path on protected `main`, routes one handoff
job to the dedicated self-hosted runner through the protected
`pipeline-integration-jenkins` environment, invokes the repository-owned Jenkins
handoff command, publishes available evidence, and renders the validated final
summary.

The checkout is a depth-1 blobless partial clone with a non-cone sparse
checkout that omits the unused `CV/` Gazebo assets, nearly all of the
repository's bytes. The CLI reads committed blobs with lazy fetching disabled,
so it fails closed outside that set. The runner reuses its checkout directory
and `actions/checkout` appends the non-cone patterns on every run, so changing
them requires a one-time wipe of that directory; removing them does not.

See `docs/pipeline-integration-trigger.md` for protection, trigger, observation,
retrieval, failure, and cancellation procedures. Jenkins remains private and no
workflow caller can supply its endpoint, credentials, job, or safety limits.
