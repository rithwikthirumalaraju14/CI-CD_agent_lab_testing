"""Command-line entry points for the viewer and evidence collector."""

import argparse
import json
import os
import sys
from pathlib import Path

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.github import GitHubError
from buildtrace.models import FAILED_CONCLUSIONS
from buildtrace.service import capture_run


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Inspect GitHub Actions failures with source-linked evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local evidence viewer")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--enable-question-runs", action="store_true", help="Allow local UI clicks to start paid model and GitHub CI trials")
    inspect = commands.add_parser("inspect", help="Archive a GitHub Actions run attempt")
    inspect.add_argument("run_url")
    verify = commands.add_parser("verify", help="Check an archive's recorded SHA-256 file checksums")
    verify.add_argument("capture_id")
    from buildtrace.experiments.commands import add_commands, dispatch

    add_commands(commands)
    for command in (serve, inspect, verify):
        command.add_argument("--data-dir", type=Path, default=Path(os.environ.get("BUILDTRACE_DATA_DIR", "data")))
    args = parser.parse_args(argv)
    if args.command == "experiment":
        return dispatch(args)
    if args.command == "serve":
        from buildtrace.web import create_app

        create_app({"DATA_DIR": args.data_dir, "ENABLE_QUESTION_RUNS": args.enable_question_runs}).run(host=args.host, port=args.port, debug=False)
        return 0
    store = EvidenceStore(args.data_dir)
    try:
        if args.command == "verify":
            result = store.verify(args.capture_id)
        else:
            manifest = capture_run(args.run_url, store)
            result = {
                "capture_id": manifest["id"],
                "run_url": manifest["run_url"],
                "reported_head_sha": manifest["source_ref"],
                "conclusion": manifest["run"].get("conclusion"),
                "failed_jobs": [
                    job["name"] for job in manifest["jobs"] if job.get("conclusion") in FAILED_CONCLUSIONS
                ],
                "evidence_files": len(manifest["entries"]),
                "warnings": manifest["warnings"],
                "manifest": str(store.capture_path(manifest["id"]) / "manifest.json"),
            }
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, GitHubError, EvidenceError, OSError) as exc:
        print(f"buildtrace: {exc}", file=sys.stderr)
        return 2
