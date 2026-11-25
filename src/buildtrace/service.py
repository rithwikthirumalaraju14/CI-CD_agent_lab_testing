"""Connect collection to immutable local evidence storage."""

from collections.abc import Callable

from buildtrace.evidence import EvidenceStore
from buildtrace.github import GitHubActionsClient
from buildtrace.models import RunReference


def capture_run(
    url: str,
    store: EvidenceStore,
    github_factory: Callable[[], GitHubActionsClient] = GitHubActionsClient,
) -> dict:
    reference = RunReference.parse(url)
    with github_factory() as github:
        collected = github.collect(reference)
    return store.save(collected)
