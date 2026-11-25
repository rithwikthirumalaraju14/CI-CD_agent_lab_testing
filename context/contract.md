# CI and CD pipeline contract

Artifacts live under a source-SHA-specific dist directory. Deployment
needs successful tests and manual approval when required. Cache identity
includes OS, Python, and the lockfile content. Matrix artifacts have
unique platform, architecture, and source identities. Build metadata,
CI configuration, and source changes all require a successful build check.
Readiness requires both an open port and a healthy application response.
Tags, built artifacts, tested revisions, and deployments must agree on
immutable source and digest identities. Tested and published dependency
locks must match. A failed rollout reports failure and restores the last
healthy artifact, not merely the newest artifact.
