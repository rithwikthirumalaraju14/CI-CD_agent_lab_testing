"""Operator-only failure catalogue and hypothetical counterexample repairs.

Family IDs describe specific mechanisms, not broad subjects such as "parsing".
Related mechanisms and templated variants are explicitly exposed in catalogue().
No historical lessons, compiler output, or solver trajectories are synthesized.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from typing import Any

from . import artifacts, ingestion, service
from .model import Case, Edit, SCHEMA_VERSION, Seed, WrongRepair


APP = "src/bench_app/app.py"


def edit(before: str, after: str) -> Edit:
    return Edit(APP, before, after)


PRECEDENCE = 'config = deep_merge(config, request.get("environment", {}))\n    config = deep_merge(config, request.get("options", {}))'
REVERSED_PRECEDENCE = 'config = deep_merge(config, request.get("options", {}))\n    config = deep_merge(config, request.get("environment", {}))'
DECODE = 'decoded = source_bytes(request, root).decode(config["csv"]["encoding"])'
BAD_DECODE = 'decoded = source_bytes(request, root).decode("utf-8")'
DELAY = 'target = parsedate_to_datetime(value)\n        return max(0, int((target - now).total_seconds()))'
PATH_PARTS = r'parts = PurePosixPath(name.replace("\\", "/"))'


CASES = (
    Case("rv2-001", "train", "ingestion", "explicit-configuration-precedence",
         "Explicit options versus worker environment", "The final configuration layer wins, even when a worker sets the same key.",
         ("configuration", "environment-scope"), (edit(PRECEDENCE, REVERSED_PRECEDENCE),),
         steps=("Read the current layer order", "Restore explicit-option precedence without discarding environment settings")),
    Case("rv2-002", "train", "ingestion", "utf8-signature-decoding",
         "Signed and unsigned UTF-8 feeds", "An optional UTF-8 signature is a stream prefix, not part of a column name or all text.",
         ("encoding", "parsing"), (edit(DECODE, BAD_DECODE),),
         (WrongRepair("strip-every-signature", "A blanket text cleanup removes a valid interior U+FEFF as well as the stream prefix.",
                      (edit(BAD_DECODE, BAD_DECODE + r'.replace("\ufeff", "")'),), "oracle"),)),
    Case("rv2-003", "train", "ingestion", "decimal-tie-rounding",
         "Ledger tie rounding", "Decimal cent quantization uses the ledger's HALF_EVEN policy, not cash-export HALF_UP.",
         ("rounding", "decimal"), (edit("rounding=ROUND_HALF_EVEN", 'rounding="ROUND_HALF_UP"'),)),
    Case("rv2-004", "train", "ingestion", "offset-instant-normalization",
         "Offset-aware ingestion instants", "Converting a timestamp to UTC differs from relabeling its wall-clock timezone.",
         ("timezone", "parsing"), (edit("return parsed.astimezone(timezone.utc)", "return parsed.replace(tzinfo=timezone.utc)"),)),
    Case("rv2-005", "development", "ingestion", "recursive-profile-inheritance",
         "Nested deployment profiles", "A nested profile must retain unmentioned sibling defaults.",
         ("configuration", "merge"),
         (edit('config = deep_merge(defaults, profiles.get(request.get("profile"), {}))', 'config = dict(defaults) | profiles.get(request.get("profile"), {})'),)),
    Case("rv2-006", "development", "ingestion", "tri-state-numeric-default",
         "Explicit zero minimum", "Null means inherit while numeric zero is an explicit filter threshold.",
         ("configuration", "sentinel"),
         (edit('minimum = minimum if minimum is not None else config["rules"]["minimum"]', 'minimum = minimum or config["rules"]["minimum"]'),)),
    Case("rv2-007", "test", "ingestion", "csv-record-boundaries",
         "Multiline CSV records", "A CSV parser needs the stream's physical newlines to retain quoted multiline fields.",
         ("parsing", "csv"),
         (edit('csv.DictReader(io.StringIO(decoded, newline=""), delimiter=config["csv"]["delimiter"])', 'csv.DictReader(decoded.splitlines(), delimiter=config["csv"]["delimiter"])'),),
         variant="reader-newline-retention"),
    Case("rv2-008", "test", "ingestion", "locale-number-separators",
         "Localized thousands and decimal marks", "A localized thousands separator must be removed before decimal normalization.",
         ("parsing", "decimal", "environment-scope"),
         (edit('value = str(value).replace(config["csv"]["thousands"], "") if config["csv"]["thousands"] else str(value)', 'value = str(value)'),)),
    Case("rv2-009", "test", "ingestion", "json-decimal-token-precision",
         "JSONL exact numeric tokens", "JSON numeric tokens must reach Decimal without a binary float round trip.",
         ("parsing", "decimal"),
         (edit("json.loads(line, parse_float=Decimal)", "json.loads(line)"),)),
    Case("rv2-010", "test", "ingestion", "revision-aware-idempotence",
         "Out-of-order feed revisions", "Idempotence chooses the greatest numeric revision rather than the first arrival.",
         ("ordering", "aggregation"),
         (edit('if row["id"] not in latest or row["revision"] > latest[row["id"]]["revision"]:', 'if row["id"] not in latest:'),),
         (WrongRepair("last-arrival-wins", "Arrival-order overwrite still loses a higher revision that arrived earlier.",
                      (edit('if row["id"] not in latest:', "if True:"),)),)),
    Case("rv2-011", "test", "ingestion", "exclusive-watermark-boundary",
         "Incremental ingestion boundary", "An exclusive watermark must not reingest the instant at the boundary.",
         ("timezone", "boundary"),
         (edit('timestamp(row["timestamp"]) > watermark', 'timestamp(row["timestamp"]) >= watermark'),)),
    Case("rv2-012", "test", "ingestion", "schema-alias-selection",
         "Per-partner schema selection", "An explicitly selected schema map must not be replaced by the default column layout.",
         ("configuration", "schema"),
         (edit('root / config["schema_file"]', 'root / "context/schemas/default.json"'),)),
    Case("rv2-013", "test", "ingestion", "configuration-relative-source-origin",
         "Job-relative input source", "A feed path is relative to its configuration file, not the shell's working directory.",
         ("resource-origin", "configuration"),
         (edit('source = path.parent / settings["source"]', 'source = root / settings["source"]'),),
         steps=("Resolve the job configuration", "Resolve its relative source", "Parse the referenced feed")),
    Case("rv2-014", "test", "ingestion", "compression-magic-dispatch",
         "Extensionless compressed feed", "Compression is identified from byte signatures, not decoded text or a missing extension.",
         ("encoding", "compression"),
         (edit('if data.startswith(b"\\x1f\\x8b"):', "if False:"),)),

    Case("rv2-015", "train", "service", "wire-constructor-generations",
         "Keep a legacy peer on major one", "Dependency major and constructor signature must both match the deployed peer.",
         ("dependency", "api-compatibility", "environment-scope"),
         (Edit("ci/constraints.txt", "wirekit==1.4.0", "wirekit==2.2.0"), edit(service.CLIENT_V1, service.CLIENT_V2)),
         (WrongRepair("pin-only", "Reverting only the dependency leaves the incompatible keyword-only constructor.",
                      (Edit("ci/constraints.txt", "wirekit==2.2.0", "wirekit==1.4.0"),)),),
         environment="legacy", variant="legacy-peer-downgrade",
         steps=("Read the current peer major", "Restore a compatible dependency pin", "Restore that major's constructor API")),
    Case("rv2-016", "train", "service", "wire-constructor-generations",
         "Move the current peer to major two", "The inverse environment needs major two; blanket downgrade advice is wrong here.",
         ("dependency", "api-compatibility", "environment-scope"),
         (Edit("ci/constraints.txt", "wirekit==2.2.0", "wirekit==1.4.0"),
          Edit("pyproject.toml", "wirekit>=2,<3", "wirekit>=1,<3"), edit(service.CLIENT_V2, service.CLIENT_V1)),
         (WrongRepair("upgrade-pin-only", "An upgraded pin does not update the old constructor call.",
                      (Edit("ci/constraints.txt", "wirekit==1.4.0", "wirekit==2.2.0"),)),),
         environment="modern", variant="current-peer-upgrade",
         steps=("Identify the required peer major", "Align range and pin", "Update constructor keyword names")),
    Case("rv2-017", "train", "service", "content-type-charset-dispatch",
         "A reporting peer's declared charset", "Decode a byte response using its Content-Type charset rather than assuming UTF-8.",
         ("encoding", "api-compatibility"),
         (edit("response.body.decode(charset)", 'response.body.decode("utf-8")'),)),
    Case("rv2-018", "train", "service", "opaque-pagination-cursor",
         "A false-looking cursor is still a cursor", "Opaque cursor values are not truth-value sentinels; only null terminates pagination.",
         ("api-compatibility", "sentinel"), (edit("if cursor is None:", "if not cursor:"),)),
    Case("rv2-019", "development", "service", "response-presence-versus-truth",
         "An HTTP error response is present", "A false Response object can carry a real status and error body.",
         ("api-compatibility", "sentinel"),
         (edit("response.status if response is not None else 599", "response.status if response else 599"),)),
    Case("rv2-020", "development", "service", "conditional-response-body-contract",
         "Not-modified has no JSON body", "A 304 reuses cached content while a 200 must decode the new body.",
         ("api-compatibility", "cache"),
         (edit('if operation == "conditional" and response.status == 304:', 'if operation == "conditional" and response.status == 204:'),),
         (WrongRepair("always-use-cache", "Unconditionally reusing the cache discards a fresh successful response.",
                      (edit('if operation == "conditional" and response.status == 204:', 'if operation == "conditional":'),)),)),
    Case("rv2-021", "test", "service", "dependency-constraint-intersection",
         "Project range and deployment lock disagree", "A valid installed API cannot rescue an unsatisfiable project/constraint intersection.",
         ("dependency", "resolver"), (Edit("pyproject.toml", "wirekit>=2,<3", "wirekit>=1,<2"),),
         (WrongRepair("downgrade-lock", "Matching the old project range breaks the deployment's required major and constructor.",
                      (Edit("ci/constraints.txt", "wirekit==2.2.0", "wirekit==1.4.0"),)),),
         steps=("Diagnose the resolver intersection", "Use the current deployment's required major without downgrading the peer")),
    Case("rv2-022", "test", "service", "same-symbol-import-origin",
         "Transport versus application signing", "Identically named signing helpers have different canonicalization contracts.",
         ("import-origin", "encoding"),
         (edit("from .signatures import sign_payload", "from wirekit import sign_payload"),)),
    Case("rv2-023", "test", "service", "canonical-idempotency-serialization",
         "Stable nested request identity", "Idempotency hashes require canonical serialization including nested objects and UTF-8.",
         ("serialization", "ordering"),
         (edit('json.dumps(request["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)', 'json.dumps(request["payload"], sort_keys=False, separators=(",", ":"), ensure_ascii=False)'),),
         (WrongRepair("sort-top-level-only", "Sorting the outer mapping leaves nested insertion order in the request identity.",
                      (edit('json.dumps(request["payload"], sort_keys=False, separators=(",", ":"), ensure_ascii=False)', 'json.dumps(dict(sorted(request["payload"].items())), sort_keys=False, separators=(",", ":"), ensure_ascii=False)'),), "oracle"),)),
    Case("rv2-024", "test", "service", "http-date-retry-after",
         "Retry-After absolute date", "HTTP-date retry instructions must be interpreted relative to the current supplied instant.",
         ("parsing", "timezone", "retry"), (edit(DELAY, "return 0"),),
         (WrongRepair("fixed-date-fallback", "A fixed 30-second fallback matches one peer sample but not other dates or past deadlines.",
                      (edit("except ValueError:\n        return 0", "except ValueError:\n        return 30"),), "oracle"),)),
    Case("rv2-025", "test", "service", "bounded-retry-exhaustion",
         "Exhaustion must remain an error", "The initial request consumes one attempt, and exhausted retries cannot become a success value.",
         ("retry", "resource-lifetime"),
         (edit('raise ValueError("retry exhausted")', 'return {"status": status, "attempts": max_attempts}'),)),
    Case("rv2-026", "test", "service", "stream-context-lifetime",
         "Consume the body before closing", "A streamed response's body is unavailable after its owner context exits.",
         ("resource-lifetime", "api-compatibility"),
         (edit('with client.open(request["body"].encode("utf-8")) as response:\n            body = response.body', 'with client.open(request["body"].encode("utf-8")) as response:\n            pass\n        body = response.body'),),
         steps=("Trace response ownership", "Consume the stream inside its lifetime", "Decode the retained bytes")),
    Case("rv2-027", "test", "service", "repeated-query-parameter-encoding",
         "List-valued query parameters", "List parameters are repeated encoded key/value pairs, not a Python list string.",
         ("encoding", "serialization"), (edit("doseq=True", "doseq=False"),)),
    Case("rv2-028", "test", "service", "partial-batch-outcome-preservation",
         "Mixed batch outcomes", "An accepted subset must not erase per-item failures or turn partial success into full success.",
         ("aggregation", "api-compatibility"),
         (edit('failures = [item["id"] for item in request["results"] if item["status"] >= 400]', "failures = []"),)),

    Case("rv2-029", "train", "artifacts", "installed-package-resource-origin",
         "Resources after installation", "A runtime package resource is not a path relative to the checkout or caller's working directory.",
         ("resource-origin", "build"),
         (edit('files("bench_app").joinpath("data/policy.json").read_text(encoding="utf-8")', 'open("src/bench_app/data/policy.json", encoding="utf-8").read()'),),
         steps=("Compare editable and installed execution", "Locate the installed package resource", "Retain working-directory independence")),
    Case("rv2-030", "train", "artifacts", "untrusted-html-escaping",
         "Diagnostic text in HTML", "Untrusted diagnostic text must be escaped for both markup and quotes.",
         ("serialization", "escaping"),
         (edit('html.escape(request.get("text", ""), quote=True)', 'request.get("text", "")'),)),
    Case("rv2-031", "train", "artifacts", "ordered-manifest-framing",
         "Deterministic framed manifests", "An artifact manifest hashes sorted filename/content frames rather than mapping insertion order.",
         ("ordering", "hashing"), (edit("for name in sorted(entries):", "for name in entries:"),)),
    Case("rv2-032", "train", "artifacts", "reproducible-archive-clock",
         "Reproducible archive metadata", "A build's wall-clock date must not leak into reproducible member metadata.",
         ("build", "reproducibility"),
         (edit('entry = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))', 'entry = zipfile.ZipInfo(name, tuple(request["built_at"]))'),)),
    Case("rv2-033", "development", "artifacts", "distribution-resource-selection",
         "Wheel data manifest", "Package include patterns must retain runtime JSON, even when source tests already pass.",
         ("build", "metadata", "resource-origin"),
         (Edit("pyproject.toml", 'wheel_include = ["*.py", "*.json"]', 'wheel_include = ["*.py"]'),),
         variant="wheel-manifest",
         steps=("Build the wheel", "Inspect its resource manifest", "Verify installed execution away from src")),
    Case("rv2-034", "development", "artifacts", "distribution-resource-selection",
         "Source distribution data manifest", "A directly built wheel can pass while the sdist omits data needed by rebuilt wheels.",
         ("build", "metadata", "resource-origin"),
         (Edit("pyproject.toml", 'sdist_include = ["*.toml", "ci/*.py", "src/bench_app/*.py", "src/bench_app/data/*.json"]', 'sdist_include = ["*.toml", "ci/*.py", "src/bench_app/*.py"]'),),
         variant="sdist-manifest",
         steps=("Build wheel and sdist", "Rebuild the sdist in a clean directory", "Preserve runtime data in both manifests")),
    Case("rv2-035", "test", "artifacts", "cross-platform-archive-path-validation",
         "Archive paths from another OS", "Archive validation normalizes both slash conventions before rejecting unsafe paths.",
         ("resource-origin", "archive"), (edit("if invalid_path:", "if False and invalid_path:"),),
         (WrongRepair("posix-only-rejection", "A POSIX-only repair misses parent traversal encoded with Windows separators.",
                      (edit("if False and invalid_path:", "if invalid_path:"), edit(PATH_PARTS, "parts = PurePosixPath(name)")), "oracle"),)),
    Case("rv2-036", "test", "artifacts", "csv-record-boundaries",
         "Quoted CSV report cells", "Rendering CSV must preserve delimiter, quote and newline boundaries just as parsing must.",
         ("serialization", "csv"),
         (edit('writer.writerows(request["rows"])', r'stream.write("\r\n".join(",".join(str(value) for value in row) for row in request["rows"]) + "\r\n")'),),
         variant="writer-cell-quoting"),
    Case("rv2-037", "test", "artifacts", "iso-duration-unit-scaling",
         "Mixed-unit report durations", "ISO duration components need their own hour/minute/second scales before millisecond conversion.",
         ("parsing", "units"), (edit("total = hours * 3600 + minutes * 60 + seconds", "total = hours + minutes + seconds"),)),
    Case("rv2-038", "test", "artifacts", "unicode-normalized-group-identity",
         "Canonical Unicode report groups", "Canonically equivalent labels share a group without case folding unrelated names.",
         ("encoding", "aggregation"), (edit('name = unicodedata.normalize("NFC", row["name"])', 'name = row["name"]'),)),
    Case("rv2-039", "test", "artifacts", "count-weighted-report-aggregation",
         "Unequal report group weights", "Group means must be weighted by their observation counts.",
         ("aggregation", "decimal"),
         (edit('total = sum(Decimal(str(row["mean_ms"])) * row["count"] for row in request["rows"])', 'total = sum(Decimal(str(row["mean_ms"])) for row in request["rows"])'),
          edit("(total / count).quantize", '(total / len(request["rows"])).quantize'))),
    Case("rv2-040", "test", "artifacts", "semantic-release-ordering",
         "Release and prerelease ordering", "Semantic version precedence differs from lexical string ordering and ignores build metadata.",
         ("ordering", "parsing"), (edit('sorted(request["versions"], key=version_key)', 'sorted(request["versions"], key=str)'),)),
    Case("rv2-041", "test", "artifacts", "binary-artifact-hash-domain",
         "Hash original artifact bytes", "Binary artifact integrity hashes cannot normalize or replace bytes via text decoding.",
         ("encoding", "hashing"),
         (edit('data = base64.b64decode(request["base64"], validate=True)', 'data = base64.b64decode(request["base64"], validate=True).decode("utf-8", errors="replace").encode("utf-8")'),)),
    Case("rv2-042", "test", "artifacts", "canonical-archive-member-uniqueness",
         "Ambiguous repeated archive members", "Duplicate detection uses the same normalized path identity as downstream consumers.",
         ("archive", "ordering"), (edit("if canonical in seen:", "if name in seen:"),)),
)

# A separate development-only difficulty probe. These cases are not part of the
# original 42-case catalogue or its untouched test split. They combine previously
# observed dependency lessons with new multi-failure deployment snapshots.
HARD_DEVELOPMENT_CASES = (
    Case("hard-dev-001", "development", "service", "modern-resolution-api-and-cursor",
         "Current peer dependency migration with pagination", 
         "A resolver mismatch masks an incompatible constructor, then a false-looking cursor exposes a separate behavior failure.",
         ("dependency", "api-compatibility", "pagination", "multi-stage"),
         (Edit("ci/constraints.txt", "wirekit==2.2.0", "wirekit==1.4.0"),
          edit(service.CLIENT_V2, service.CLIENT_V1),
          edit("if cursor is None:", "if not cursor:")),
         environment="modern_opaque",
         steps=("Reconcile the deployed major and resolver", "Adapt its constructor", "Preserve opaque cursor semantics")),
    Case("hard-dev-002", "development", "service", "legacy-version-api-and-charset",
         "Legacy peer dependency repair with declared charset",
         "A wrong major masks an incompatible constructor, then a declared non-UTF8 response exposes a separate decoding failure.",
         ("dependency", "api-compatibility", "encoding", "multi-stage"),
         (Edit("ci/constraints.txt", "wirekit==1.4.0", "wirekit==2.2.0"),
          edit(service.CLIENT_V1, service.CLIENT_V2),
          edit("response.body.decode(charset)", 'response.body.decode("utf-8")')),
         environment="legacy_opaque",
         steps=("Reconcile the legacy major", "Adapt its constructor", "Honor the response charset")),
)


def _hard_test(case_id: str, family: str, components: tuple[str, ...]) -> Case:
    sources = [next(case for case in CASES if case.id == source_id) for source_id in components]
    if len({case.domain for case in sources}) != 1 or any(case.split != "test" for case in sources):
        raise ValueError("Hard held-out components must share a domain and test split")
    return Case(
        case_id, "test", sources[0].domain, family,
        "Multi-stage " + sources[0].domain + " failure",
        "Several independent defects in one deployment require a complete verified repair.",
        tuple(dict.fromkeys(("multi-stage", *(tag for case in sources for tag in case.tags)))),
        tuple(change for case in sources for change in case.edits),
        steps=tuple(case.title for case in sources),
    )


# Defined before the development probe is run; these held-out snapshots are not
# materialized or evaluated unless the original-source-use gate passes.
HARD_TEST_CASES = (
    _hard_test("hard-test-001", "csv-locale-json-revisions", ("rv2-007", "rv2-008", "rv2-009", "rv2-010")),
    _hard_test("hard-test-002", "watermark-schema-origin-compression", ("rv2-011", "rv2-012", "rv2-013", "rv2-014")),
    _hard_test("hard-test-003", "csv-watermark-schema-compression", ("rv2-007", "rv2-011", "rv2-012", "rv2-014")),
    _hard_test("hard-test-004", "locale-json-revisions-origin", ("rv2-008", "rv2-009", "rv2-010", "rv2-013")),
    _hard_test("hard-test-005", "resolver-signature-key-retry-date", ("rv2-021", "rv2-022", "rv2-023", "rv2-024")),
    _hard_test("hard-test-006", "retry-stream-query-batch", ("rv2-025", "rv2-026", "rv2-027", "rv2-028")),
    _hard_test("hard-test-007", "resolver-retry-stream-query", ("rv2-021", "rv2-025", "rv2-026", "rv2-027")),
    _hard_test("hard-test-008", "signature-key-date-batch", ("rv2-022", "rv2-023", "rv2-024", "rv2-028")),
    _hard_test("hard-test-009", "tar-csv-duration-unicode", ("rv2-035", "rv2-036", "rv2-037", "rv2-038")),
    _hard_test("hard-test-010", "weighted-release-digest-duplicate", ("rv2-039", "rv2-040", "rv2-041", "rv2-042")),
    _hard_test("hard-test-011", "tar-weighted-release-digest", ("rv2-035", "rv2-039", "rv2-040", "rv2-041")),
    _hard_test("hard-test-012", "csv-duration-unicode-duplicate", ("rv2-036", "rv2-037", "rv2-038", "rv2-042")),
)


def get_case(case_id: str) -> Case:
    for case in (*CASES, *HARD_DEVELOPMENT_CASES, *HARD_TEST_CASES):
        if case.id == case_id:
            return case
    raise KeyError(f"Unknown research benchmark case: {case_id}")


def get_seed(case: Case) -> Seed:
    if case.domain == "ingestion":
        return ingestion.seed()
    if case.domain == "service":
        return service.seed(case.environment)
    if case.domain == "artifacts":
        return artifacts.seed()
    raise ValueError(f"Unknown project domain: {case.domain}")


def validate_catalogue() -> None:
    if len({case.id for case in CASES}) != len(CASES):
        raise ValueError("Duplicate case identity")
    if Counter(case.split for case in CASES) != {"train": 12, "development": 6, "test": 24}:
        raise ValueError("Unexpected split sizes")
    family_splits: dict[str, set[str]] = defaultdict(set)
    snapshots: set[str] = set()
    for case in CASES:
        family_splits[case.family].add(case.split)
        seed = get_seed(case)
        broken = dict(seed.files)
        for change in case.edits:
            if not (change.path.startswith("src/bench_app/") or change.path in {"pyproject.toml", "ci/constraints.txt"}):
                raise ValueError(f"Mutation touches protected path: {case.id}: {change.path}")
            change.apply(broken)
        fingerprint = hashlib.sha256(b"".join(name.encode() + b"\0" + data for name, data in sorted(broken.items()))).hexdigest()
        if fingerprint in snapshots:
            raise ValueError(f"Duplicate broken snapshot: {case.id}")
        snapshots.add(fingerprint)
        for repair in case.wrong_repairs:
            candidate = dict(broken)
            for change in repair.edits:
                change.apply(candidate)
        for change in reversed(case.edits):
            change.inverse().apply(broken)
        if broken != seed.files:
            raise ValueError(f"Non-reversible reference solution: {case.id}")
    overlap = {family: sorted(splits) for family, splits in family_splits.items() if len(splits) > 1}
    if overlap:
        raise ValueError(f"Families cross splits: {overlap}")


def catalogue() -> dict[str, Any]:
    validate_catalogue()
    families: dict[str, list[str]] = defaultdict(list)
    tags: dict[str, set[str]] = defaultdict(set)
    for case in CASES:
        families[case.family].append(case.id)
        for tag in case.tags:
            tags[tag].add(case.split)
    entries = []
    for case in CASES:
        seed = get_seed(case)
        category = "dependency" if "dependency" in case.tags else (
            "import" if "import-origin" in case.tags else ("configuration" if "configuration" in case.tags else "test")
        )
        entries.append(case.summary() | {
            "expected_public_tests": len(seed.public),
            "expected_oracle_tests": len(seed.oracle),
            "current_evidence": ["context/deployment.json", "context/contract.md", "tests/test_application.py", "pyproject.toml", "ci/constraints.txt"],
            "experiment_fields": {
                "id": case.id, "family": case.family, "split": "dev" if case.split == "development" else case.split,
                "category": category, "expected_test_count": len(seed.public), "editable_environment_keys": [],
            },
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "operator draft; development validation and held-out freeze are coordinator decisions",
        "provenance": "synthetic authored failures and counterexample repairs; no recorded agent runs or memory lessons",
        "counts": {"cases": len(CASES), "families": len(families), "domains": 3, "splits": dict(Counter(case.split for case in CASES))},
        "shared_families": {family: ids for family, ids in families.items() if len(ids) > 1},
        "family_scope": "Specific failure mechanisms are split-disjoint. Broad subjects and project code are intentionally shared; this is not project-held-out or broad-concept-held-out evaluation.",
        "coarse_tags_crossing_splits": {tag: sorted(splits) for tag, splits in sorted(tags.items()) if len(splits) > 1},
        "transfer_hypotheses_not_observations": [
            {"source_cases": ["rv2-015", "rv2-016"], "target_cases": ["rv2-021"], "scope_conflict": "Universal upgrade/downgrade advice ignores the current peer and resolver intersection."},
            {"source_cases": ["rv2-002"], "target_cases": ["rv2-041"], "scope_conflict": "Text-stream signature cleanup is inappropriate for byte-exact artifact hashes."},
            {"source_cases": ["rv2-004"], "target_cases": ["rv2-011", "rv2-024"], "scope_conflict": "UTC normalization alone does not define watermark inclusivity or Retry-After date arithmetic."},
            {"source_cases": ["rv2-029"], "target_cases": ["rv2-033", "rv2-034"], "scope_conflict": "A resource lookup repair does not put missing resources into distribution manifests."},
            {"source_cases": ["rv2-031"], "target_cases": ["rv2-023", "rv2-040"], "scope_conflict": "Filename sorting does not establish recursive JSON canonicalization or semantic version precedence."},
        ],
        "cases": entries,
    }
