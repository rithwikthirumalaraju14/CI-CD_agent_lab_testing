# Failed-five learning question repair

Status: **complete for these five replacements**. The pre-existing 45 question tasks, their trials, and the original learning-50 corpus were not edited.

## Admission result

| Original failed question | Replacement | No-memory verified solves | Corpus admission |
| --- | --- | ---: | --- |
| L06: dependency major and query keys | V2L06 | 3/3 | admitted |
| L25: archive metadata and byte hashing | V3L25 | 3/3 | admitted |
| L27: canonical duplicate TAR members | V3L27 | 3/3 | admitted |
| L30: cross-OS unsafe TAR paths | V3L30 | 3/3 | admitted |
| L45: required build gate | V2L45 | 3/3 | admitted |

Each counted solve passed both the public CI checks and the trusted oracle. V2L25, V2L27, and V2L30 each failed 0/3 and were excluded; their narrower V3 replacements passed. The admission rule was at least one verified solve in three independent No-memory attempts. All 15 admitted sessions were successful.

The new five-question WikiTrace corpus is `CI-CD_agent_lab_testing/output/learning50-final/memory-failed-five-combined/`. It has 15 raw solver histories, original CI evidence, 15 compiled trajectories, and 31 linked insights. All 31 insights passed linked compilation and raw-document reads. Source corpus hashes and the original corpus hash are in `merge-provenance.json`.

The original learning-50 corpus remains at `CI-CD_agent_lab_testing/output/learning50-final/memory-learning50-v1/`; its manifest SHA-256 is `298ae33650de267b38b0fec0e57eb93abbb14907952b5d0a46d1d65f548db25f`. This supplement is separate and has not been attached to a development or held-out evaluation. The old 125/150 (83.3%) calibration still describes the original questions; it is not a new baseline for the revised five.

The earlier goals of 55–85% overall success and at least one success for every question cannot both be inferred from the frozen 45 plus replacements: the original 45 already account for 125 successes, and one success on each of five replacements would make at least 130/150 (86.7%). This round prioritizes the requested five-question admission rule; no mixed-version 50-question score is reported as a fresh baseline.

## Recorded usage and estimated cost

Across all 24 diagnostic trials (including the nine excluded V2 failures): 293 solver model calls, 2,878,827 input tokens (2,564,942 cached), and 54,757 output tokens; estimated **$0.084416**.

Across 15 WikiTrace compiler calls: 310,097 input tokens (0 cached) and 8,786 output tokens; estimated **$0.035403**. Combined estimate: **$0.119819**.

Prices: [GPT-6 Luna official model page](https://developers.openai.com/api/docs/models/gpt-6-luna), USD per million text tokens: $0.10 input, $0.01 cached input, $0.50 output. Reasoning tokens are included in output. This is a model-token estimate from recorded usage, not an invoice; cache-write premiums and GitHub billing are not included.

Every attempt, outcome, CI/oracle URL, token count, cost estimate, and trajectory path is in `FAILED_FIVE_ATTEMPTS.csv`; machine-readable totals are in `FAILED_FIVE_USAGE.json`.
