# Fixture Cases

Immutable deterministic Fixture cases are committed here as pipeline-interface
evidence. A Fixture never claims Domain behavior, Validation evidence, or KPI
success.

Each case is selected by exact declared input byte sizes and SHA-256 digests. Its
payload bytes and diagnostic are independently checked before the adapter
publishes them. The C-01 cases emit schema-valid composition, image-build, CV,
and CD contract data with Domain outcome `not_evaluated`. Their exact bytes prove
only the four-Stage pipeline interface and handoff graph.
