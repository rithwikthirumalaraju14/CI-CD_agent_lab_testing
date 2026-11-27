# Pipeline application

This is a standalone offline project. No network service or credentials
are required. Read contract.md and deployment.json for the current
deployment; legacy deployment notes describe other supported contexts.
Application entry point: python -m bench_app examples/input.json.
Tooling is provisioned by the runner (Python 3.11+, pytest 8.3.5,
build 1.2.2.post1). Use ci/constraints.txt for project installation.
ci/_build_backend.py is an auditable stdlib-only PEP 517/660 backend.
Build both wheel and sdist; installed packages must work away from the
checkout. tests/, context/, examples/, vendor/ and ci tooling are
protected; src/bench_app/, pyproject.toml and ci/constraints.txt are editable.
