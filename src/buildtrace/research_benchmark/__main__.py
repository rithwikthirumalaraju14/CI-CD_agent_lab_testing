"""Standalone operator CLI; intentionally not wired into experiment core."""

from __future__ import annotations

import argparse
import json

from . import catalogue, materialize_case, verify_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operator-only offline research benchmark generator")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="Print the full operator catalogue as JSON")
    materialize = commands.add_parser("materialize", help="Create a new standalone case directory")
    materialize.add_argument("case_id")
    materialize.add_argument("destination")
    materialize.add_argument("--verifier-dir")
    materialize.add_argument("--correct-baseline", action="store_true")
    verify = commands.add_parser("verify", help="Run the complete local offline verification contract")
    verify.add_argument("project")
    verify.add_argument("--verifier-dir", required=True)
    verify.add_argument("--work-dir")
    args = parser.parse_args(argv)
    if args.command == "catalog":
        value = catalogue()
        status = 0
    elif args.command == "materialize":
        value = materialize_case(args.case_id, args.destination, correct_baseline=args.correct_baseline, verifier_dir=args.verifier_dir).to_dict()
        status = 0
    else:
        result = verify_snapshot(args.project, args.verifier_dir, work_dir=args.work_dir)
        value = result.to_dict()
        status = 0 if result.passed else 1
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
