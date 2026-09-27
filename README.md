# Remediation Reversals in Vulnerability Advisories

A frozen-data, offline replication of the longitudinal npm and PyPI advisory
analysis by Cemil Emre Yavas, Nancy R. Mead, and Lei Chen.

## Reproduce the experiment

Use Python 3.12. Create an isolated environment, install the pinned dependencies,
and run the full pipeline from this directory:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py --workers 8
```

On Windows, activate with `.venv\Scripts\activate` instead. After dependencies
are installed, the pipeline uses the frozen local data and runs offline.
`--workers` controls parallel history processing. Eight workers are the default;
`--workers 12` uses all 12 CPU cores on a machine with sufficient available memory.
Numerical library thread counts are limited within each process to avoid
oversubscription. The workload consists of version parsing, historical replay,
and vectorized statistical resampling and uses CPUs.

The runner verifies input hashes, executes 17 validation tests, reconstructs the
histories and candidate decisions, runs the statistical analysis and policy
replay, and performs source-trace and sensitivity audits. It then compares every
result with the frozen expected content hashes. A mismatch produces a nonzero
exit status. Stage logs are written to `logs/`. Check already-generated results
with `python run.py --verify-only`.

The environment was tested with Python 3.12 on macOS with Apple silicon.
`requirements.txt` pins the numerical and version-parsing dependencies. The
package-cluster bootstrap uses 5,000 draws and seed 260927. Results are ordered
deterministically. For compressed outputs, validation hashes the decompressed
bytes so that gzip header timestamps do not affect reproducibility.

## Frozen inputs and provenance

- `data/source_manifest.json`: source repository, frozen commit, observation
  cutoff, extraction time, ecosystem selection, and source license.
- `data/blobs.jsonl.gz`: 47,821 original advisory JSON objects, each stored as
  `sha` (Git blob identifier) and `text` (original JSON bytes decoded as UTF-8).
  Preparation recomputes the Git object identifier before using each record.
- `data/events.jsonl.gz`: the selected first-parent file-change history. Each
  row identifies the advisory, commit, repository order, commit time, path,
  and old/new object identifiers. The all-zero object identifier denotes a
  missing side of an addition or deletion. Same-commit moves are resolved by
  advisory identifier before reconstruction.
- `data/registry/`: 1,689 public registry observations, keyed by ecosystem and
  package, with release timestamps, source URL, retrieval timestamp, response
  status, and original-response SHA-256. PyPI uses the earliest retained
  distribution-upload time for each release. Version matching follows the
  ecosystem's version equivalence rules.
- `input_checksums.json`: SHA-256 hashes of all frozen input files.

The source is GitHub's reviewed advisory history, frozen at commit
`7ccbff3744047c616177c0e7e39cfa6dd0623c0e`, timestamp
`2026-09-27T12:36:00+00:00`. Terminal observed npm/PyPI membership selects the
13,760-advisory source corpus; deleted records use their last extant state.
The selected history contains 57,384 repository observations.

## Analysis definitions

The primary inception cohort is first observed from 1 June 2022 through
31 December 2025, with a nonnegative publication-to-observation lag of at most
30 days. A decision is an advisory–package–version tuple whose stable version
appears as a fixed boundary in the first observed record, is outside the union
of affected intervals in that record, and has registry evidence of release by
the decision time. The inventory and chosen version are held fixed.

`versioning.py` handles npm SemVer and PyPI PEP 440 ordering. It combines
repeated package entries and evaluates standardized OSV `introduced`, `fixed`,
`last_affected`, and explicit `versions` fields. Lower bounds are inclusive,
fixed upper bounds exclusive, and last-affected upper bounds inclusive.
Provider-specific range annotations are retained in source records and excluded
from the core predicate. Unsupported or malformed predicates produce an unknown
state. Withdrawal, deletion, package absence, and unknown predicates end the
primary observation interval. Such states do not mean unaffected.

A reversal is the first subsequent affected classification of the selected
version. It measures recorded classification, not demonstrated exploitability,
observed deployment, or independently verified patch failure. Counts with full
observed follow-up and complete 7/30/90/180/365-day follow-up answer different
questions. Bootstrap intervals resample ecosystem–package clusters; the
Kaplan–Meier series is descriptive.

The 180-day replay compares no checking, UTC-anchored periodic checking, and
FIFO semantic-revision delivery with zero, one-hour, or 24-hour delay. It counts
predicate evaluations per decision and integrates stale-unaffected and
stale-affected decision-days. The semantic signature includes normalized
intervals, package identities, record lifecycle state, and parse errors. It is
conservative: changes can cause a recheck without changing the selected
version's classification. Initial evaluations are excluded from work counts.
Source-acquisition cost, exploitability, and analyst handling time are separate
from these metrics. Twenty-four phase offsets are tested for each polling period.

Sensitivity analyses vary the publication lag and include later distinct fixed
targets. Exploratory checks exclude histories with provider-specific range
annotations and allow re-entry after deletion gaps of at most 24 hours. The
re-entry check compares event counts and does not interpolate duration outcomes
across missing observations.

## Main reproduced results

| Quantity | Result |
|---|---:|
| Release-validated initial decisions | 4,130 |
| Distinct contributing advisories / packages | 2,653 / 1,289 |
| Decisions with a reversal | 30 (29 advisories) |
| Observed reversal proportion | 0.73% |
| Package-cluster 95% bootstrap interval | 0.45–1.09% |
| Median / maximum time to reversal | 4.83 / 769.77 days |
| Reversals after excluding custom-range histories | 21 / 3,696 |
| Daily replay: predicate rechecks / stale-unaffected days | 642,717 / 7.19 |
| One-hour revision replay: rechecks / stale-unaffected days | 253 / 1.00 |
| 24-hour revision replay: rechecks / stale-unaffected days | 251 / 23.79 |

## Code and result files

| File | Purpose |
|---|---|
| `src/versioning.py` | Core interval evaluator and separate event-state evaluator |
| `src/prepare.py` | Hash validation, ordered history reconstruction, cohort and target construction |
| `src/analyze.py` | Release validation, bootstrap intervals, first-event series, policy replay, selection sensitivity |
| `src/audit_results.py` | Case traces, record-edit taxonomy, censoring, custom-range and deletion sensitivities, semantic-filter audit |
| `src/test_versioning.py` | Boundary and normalization cases plus 202,000 randomized membership comparisons |
| `src/test_analysis.py` | Censoring, release-version equivalence, replay timing, and work-accounting tests |
| `results/summary.json` | Primary corpus, ecosystem, reversal, and workload counts |
| `results/audit_summary.json` | Independent trace checks and robustness summaries |
| `results/decisions.csv` | Every primary decision, release time, follow-up, censoring, first reversal, and terminal observed state |
| `results/reversals.csv` | Primary decisions with a reversal |
| `results/case_traces.csv` | Adjacent pre/post-reversal records, source hashes, references, and observed episode lengths |
| `results/horizon_rates.csv` | Complete-horizon proportions and package-cluster intervals |
| `results/policy_replay.csv` | Aggregate rechecking, alerts, and stale-classification time by policy |
| `results/policy_decisions.csv.gz` | Decision-level replay metrics for every policy |
| `results/polling_phase_sensitivity.csv` | Periodic-policy outcomes for 24 phase offsets |
| `results/sensitivity.csv` | Publication-lag and later-target sensitivity estimates |
| `results/time_to_reversal.csv` | Descriptive Kaplan–Meier risk sets and cumulative event estimates |
| `results/cohort_counts.csv`, `results/yearly_rates.csv` | Ecosystem cohort counts and inception-year summaries |
| `results/revision_types.csv` | Mutually exclusive structured-edit taxonomy |
| `results/excluded_initial_decisions.csv` | Release-validation exclusions and their reasons |
| `results/decision_histories.jsonl.gz` | Supported primary decision states, transition events, and revision actions |
| `results/candidates.jsonl.gz` | Stable initially unaffected fixed targets and their complete source trajectories |
| `results/advisories.jsonl.gz`, `results/revision_events.jsonl.gz` | Advisory summaries and chronological change records |
| `results/preparation_counts.json`, `results/registry_packages.json` | Preparation checks and package index |
| `expected_results.json` | Expected SHA-256 digest of every numeric output; compressed files use decompressed content |

Times in the numeric tables are UTC Unix seconds unless a column explicitly
ends in `_days`. Proportions ending in `_pct` are percentages. The `initial`
flag distinguishes first-record targets from later distinct targets. `censor`
identifies the reason observation ends. `endpoint_affected` refers to the last
supported state before censoring, rather than necessarily the frozen latest
record. `series` stores every supported snapshot; `transitions` stores membership
changes; `revision_actions` stores semantically changed record deliveries.
A blank `lag_limit_days` denotes unrestricted nonnegative publication lag.
Advisory counts can overlap across ecosystems; ecosystem–package counts do not.

## Licensing and citation

Original analysis code and documentation: MIT (`LICENSE`). GitHub advisory
records: CC BY 4.0, with the original source license under `LICENSES/`.
`THIRD_PARTY.md` records source attribution and the treatment of public registry
facts. `CITATION.cff` supplies software citation metadata.
