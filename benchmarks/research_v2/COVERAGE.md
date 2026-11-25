# Research v2 catalogue coverage

**Draft benchmark construction, not a frozen study or evidence of memory gains.**
The pilot established operational feasibility but had four small held-out cases,
near-ceiling no-memory repairs and zero historical-source reads. These projects
add scope conflicts, installed-artifact diagnostics and counterexamples. Their
effect on agent difficulty or evidence use still needs development evaluation.

## Actual coverage

| Offline project | Train | Development | Held-out test | Public tests/case | Oracle tests/distribution |
| --- | ---: | ---: | ---: | ---: | ---: |
| CSV/JSONL ingestion and configuration pipeline | 4 | 2 | 8 | 21 | 12 |
| Versioned service/client fixture adapter | 4 | 2 | 8 | 20 | 14 |
| Artifact/release/report processor | 4 | 2 | 8 | 18 | 16 |
| Total scenarios | **12** | **6** | **24** | | |

There are **42 scenarios and 39 specific mechanism families**: 11 training,
5 development and 23 held-out families. Three shared families are deliberately
counted once and kept wholly in one split:

- `wire-constructor-generations`: rv2-015/016, opposing legacy/current dependency
  major plus constructor contexts (training).
- `distribution-resource-selection`: rv2-033/034, wheel versus sdist include
  manifest variants (development).
- `csv-record-boundaries`: rv2-007/036, input multiline retention versus output
  cell quoting (test). These are related parser/renderer variants, not two
  independent broad CSV families.

All other family IDs represent different operations or causal mechanisms,
not renamed constants/files. All 42 broken byte snapshots are distinct. The
three applications share code across their own cases; the service also has two
healthy API environments. This is **specific-failure-family-held-out**, not
project-held-out or broad-concept-held-out. Encoding, ordering, parsing, API and
resource concepts intentionally recur across splits; the executable catalogue
reports every coarse tag that crosses splits.

`catalogue.json` is a compact operator index. The Python `catalogue()` API and
`python -m buildtrace.research_benchmark catalog` add exact mechanisms, multi-step
repair descriptions, current-evidence locations, hypothetical wrong repairs,
per-case counts, variants and core-field mappings.

### Ingestion

Configuration layer precedence; UTF-8 signature boundaries; Decimal tie policy;
UTC instant conversion; recursive profile inheritance; explicit zero versus
null; multiline CSV; locale thousands/decimal separators; JSON numeric-token
precision; out-of-order revisions; exclusive watermark; selected schema aliases;
configuration-relative source origin; extensionless gzip dispatch.

### Service/client

Opposing v1/v2 dependency/constructor contexts; declared response charset;
opaque false-looking cursors; response presence versus boolean truth; 304 cache
semantics; resolver range/lock intersection; same-named helpers from different
import origins; nested canonical idempotency serialization; HTTP-date Retry-After;
bounded retry exhaustion; stream ownership; repeated query parameters; partial
batch outcomes. The wirekit fixture is a real local installable wheel but a
**synthetic SDK**, not a claim about a particular external library or outage.

### Artifacts/reports

Installed resource origin; HTML escaping; framed sorted manifest hashes;
reproducible ZIP metadata; wheel/sdist data manifests; cross-platform TAR path
validation; CSV cell rendering; ISO duration unit scaling; NFC group identity;
count-weighted means; semantic release precedence; byte-exact hashes; canonical
archive member uniqueness.

Multi-step cases include paired dependency/API repairs in opposite contexts,
resolving a job then its relative feed, tracing response ownership, and diagnosing
source/direct-wheel/sdist-rebuilt-wheel differences. Many other mutations are
deliberately small, realistic edits. Forty-two scenarios do not imply forty-two
large repositories or forty-two unrelated broad concepts.

## Historical-transfer design without invented memory

Current contracts, deployments, readable dependency APIs, examples and protected
tests supply all required facts. Oracle fixtures add promised edge cases, not
undocumented requirements. Deployment notes describe authored legitimate
application contexts; they are not recorded agent histories.

The operator catalogue records **hypotheses**, not observations:

- Blanket upgrade/downgrade advice from one peer can conflict with the other
  peer or a current dependency-range/lock intersection.
- A stream-prefix UTF-8 cleanup must not become byte normalization for hashes.
- UTC normalization alone does not settle watermark boundaries or HTTP dates.
- Correct package-resource lookup does not include missing data in a wheel/sdist.
- Sorting manifest filenames does not solve nested JSON identity or semantic
  release ordering.

Training runs must produce actual experiences and complete source trajectories
before any lessons are compiled. No fake lesson, failed attempt, compiler result
or evidence-read event is generated. Nothing here demonstrates a development
source-read gate, Gain, Regression, or a preferred method's win.

## Counterexamples and limits

Nine operator-only hypothetical repairs are checked: global signature stripping,
last-arrival deduplication, two pin-only API repairs, unconditional cache reuse,
wrong-major lock downgrade, top-level-only JSON sorting, a fixed Retry-After
fallback, and POSIX-only archive validation. Four pass the public tests and are
rejected by additional oracle cases. They are explicitly authored counterexamples,
not reported historical solver behavior.

The generator validates every exact edit anchor and inverse. Tests execute all
four healthy seeds, reject all 42 broken variants, check the nine counterexamples,
exercise installed distributions, enforce protected-file/count contracts, and
check export isolation. The verifier runs each oracle suite against both a
direct wheel and a clean sdist-rebuilt wheel for a healthy candidate.

True limitations: projects and failures are authored synthetic fixtures; many
patches are small; behavioral oracles are finite; there are only three shared
project domains; dependency/API drift uses a bundled fixture SDK; timezone cases
use explicit offsets/HTTP dates rather than a full IANA/DST database. Empirical
task difficulty, natural memory quality, source-reading usability, robust remote
candidate attestation and the final held-out freeze remain coordinator work.
