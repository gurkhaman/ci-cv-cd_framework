# Pipeline Integration Workflow

`pipeline-integration.yml` is the sole external Pipeline integration trigger. It
accepts one committed run-request path, `run_request_path`, and runs only on
protected `main`. Its one `handoff` job runs on the self-hosted runner labelled
`sdi-jenkins-handoff`, a Jenkins stack runner service, in the protected
`pipeline-integration-jenkins` environment. The job has a 110-minute timeout and
only `contents: read` permission.

The job first checks that the runner image's baked integration code matches the
revision. If the digests differ, the job fails and asks for a rebuild with
`stack start`. It then runs `sdi-integration handoff-jenkins`, uploads the
`pipeline-integration-<execution-id>` artifact for 90 days, and renders the
validated final summary with `sdi-integration github-summary`. The artifact
holds whichever of the bundle, `handoff-receipt.json`, and
`deployment-manifest.yaml` the run produced. A run can publish a receipt alone.

The checkout is a depth-1 blobless partial clone of the protected revision. The
CLI reads committed blobs with lazy fetching disabled, so it fails closed outside
that revision. The runner's workspace is a tmpfs, so the checkout directory
persists only until the runner container restarts; restarting it with
`stack stop` and `stack start`, or `stack quiesce` and `stack resume`, clears
it.

See `docs/pipeline-integration-trigger.md` for protection, trigger, observation,
retrieval, failure, and cancellation procedures. Jenkins remains private and no
workflow caller can supply its endpoint, credentials, job, or safety limits.
