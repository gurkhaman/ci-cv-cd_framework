# Adapter Descriptors

Reviewed descriptors select a Stage, Implementation mode, digest-pinned image,
process-contract version, Stage-profile version, agent label, entry point, and
logical secret bindings. Secret values and Jenkins credential identifiers are
governed separately and never enter these contracts.

The four Fixture descriptors receive no secrets and select one shared
digest-pinned Fixture implementation through separate reviewed Stage profiles.
Their local entry point exercises the same permanent process operation before the
Jenkins deployment and agent images are introduced.
