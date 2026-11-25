"""Prepare NEW public/trusted deploy trees from one materialized generator case.

This operator utility uses only stdlib and the sibling hosted runtime. It copies
allowlisted project/oracle files, never the generator manifest, catalogue, edits,
or a surrounding checkout. It performs no installation, Git, or HTTP operations.
"""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import time


_SPEC = importlib.util.spec_from_file_location("_hosted_runtime", Path(__file__).with_name("hosted_verify.py"))
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


def _new(path):
    if path.exists() or path.is_symlink():
        raise FileExistsError("Deployment destinations must be new")
    if not path.parent.is_dir():
        raise FileNotFoundError("Deployment destination parents must exist")


def _tree_hash(files):
    import hashlib

    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _publish(staging, destination):
    """Publish the same complete stage atomically despite brief scanner locks."""
    delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(delays) + 1):
        _new(destination)
        try:
            staging.rename(destination)
            return
        except PermissionError:
            if attempt == len(delays) or not staging.is_dir() or destination.exists():
                raise
            time.sleep(delays[attempt])


def prepare_deployment(project, verifier_dir, public_destination, trusted_destination):
    project, verifier, public, trusted = (Path(value).resolve() for value in (project, verifier_dir, public_destination, trusted_destination))
    roots = (project, verifier, public, trusted)
    if any(left.is_relative_to(right) or right.is_relative_to(left) for number, left in enumerate(roots) for right in roots[number + 1:]):
        raise ValueError("Generator inputs and deployment outputs must be disjoint")
    _new(public)
    _new(trusted)
    manifest = json.loads((verifier / "manifest.json").read_bytes())
    contract = runtime.validate_contract(copy.deepcopy(manifest["contract"]))
    runtime.check_tree(project, contract["protected_sha256"], allow_edits=True)
    # The generator manifest is operator metadata, not part of the deployed bundle.
    for path in verifier.rglob("*"):
        name = path.relative_to(verifier).as_posix()
        if path.is_symlink() or path.is_file() and name not in contract["oracle_sha256"] and name != "manifest.json":
            raise ValueError("Unexpected operator file in the input oracle bundle")
    oracle_files = {}
    for name, digest in contract["oracle_sha256"].items():
        path = runtime.safe_path(verifier, name)
        content = path.read_bytes()
        if runtime.sha256(content) != digest:
            raise ValueError("Oracle bundle differs from its generator contract")
        oracle_files[name] = content
    public_files = {}
    for path in project.rglob("*"):
        name = path.relative_to(project).as_posix()
        if runtime.ignored(name) or not path.is_file():
            continue
        # check_tree already rejected every unexpected noneditable path.
        public_files[name] = path.read_bytes()
    workflow_path = ".github/workflows/" + contract["workflow"]
    workflow = Path(__file__).with_name("hosted-ci.yml").read_bytes()
    helper = Path(__file__).with_name("hosted_verify.py").read_bytes()
    descriptor = runtime.canonical({"schema_version": runtime.PUBLIC_SCHEMA, "expected_public_tests": contract["expected_public_tests"]})
    public_files.update({workflow_path: workflow, runtime.RUNTIME_PATH: helper, runtime.PUBLIC_DESCRIPTOR: descriptor})
    contract["protected_sha256"] = {name: runtime.sha256(data) for name, data in sorted(public_files.items()) if not runtime.editable(name)}
    contract["snapshot_sha256"] = _tree_hash(public_files)
    contract["required_steps"] = copy.deepcopy(runtime.REQUIRED_STEPS)
    raw_contract = runtime.canonical(contract)
    trusted_files = {
        workflow_path: workflow, runtime.RUNTIME_PATH: helper, runtime.PUBLIC_DESCRIPTOR: descriptor,
        runtime.CONTRACT_PATH: raw_contract,
        **{f"infra/research_v2/oracle/{name}": content for name, content in oracle_files.items()},
    }
    stages = []
    try:
        for destination, files in ((public, public_files), (trusted, trusted_files)):
            staging = Path(tempfile.mkdtemp(prefix=".hosted-", dir=destination.parent))
            stages.append(staging)
            for name, content in sorted(files.items()):
                path = runtime.safe_path(staging, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            _publish(staging, destination)
    finally:
        for staging in stages:
            if staging.exists():
                shutil.rmtree(staging)
    return {
        "schema_version": 2, "artifact_prefix": "bench-ci", "workflow": contract["workflow"],
        "oracle_contract_path": runtime.CONTRACT_PATH,
        "verification_contract_sha256": runtime.sha256(raw_contract),
        "expected_test_count": contract["expected_public_tests"], "expected_oracle_test_count": contract["expected_oracle_tests"],
        "editable_paths": contract["editable_paths"], "protected_paths": contract["protected_paths"],
        "required_steps": contract["required_steps"], "editable_environment_keys": [],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "verifier-dir", "public-destination", "trusted-destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_deployment(args.project, args.verifier_dir, args.public_destination, args.trusted_destination)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Hosted deployment preparation failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
