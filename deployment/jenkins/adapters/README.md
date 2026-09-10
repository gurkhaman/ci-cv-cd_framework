# Adapter Descriptors

Reviewed descriptors select a Stage, Implementation mode, digest-pinned image,
process-contract version, Stage-profile version, agent label, entry point, and
logical secret bindings. Secret values and Jenkins credential identifiers are
governed separately and never enter these contracts.

The composition Fixture descriptor receives no secrets. Its local entry point is
used to exercise the permanent process interface before the Jenkins deployment
and agent images are introduced.
