"""Launch the existing BuildTrace CLI with one in-memory GitHub token lookup.

Credential-manager lookups can occasionally time out under parallel CI load.
This wrapper does not print, persist, or alter the token; it only lets worker
threads share the token already authorized for this process.
"""

from __future__ import annotations

import os
import sys

from buildtrace.cli import main
from buildtrace.github import resolve_github_token
from buildtrace.research_benchmark.operate import _load_environment


def run() -> int:
    _load_environment()
    token = resolve_github_token()
    if not token:
        raise RuntimeError("GitHub authentication is unavailable before the experiment starts")
    os.environ["GITHUB_TOKEN"] = token
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(run())
