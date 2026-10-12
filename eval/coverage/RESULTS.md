# Coverage corpus: a numeric case for every queryable relation

`eval/coverage/cases.json` is the broad counterpart of `eval/truth/numeric_cases.json`. Where the
truth corpus is a hand-targeted set of sharp cases, this corpus is exhaustive over the signed
manifest: the survey found 94 of 119 signed relations with zero coverage, and this file exists so a
relation or column nobody thought to target still has a case.

```json
{
  "run": "run-20261011T195359",
  "cases": 1010,
  "kinds": {"row_count": 388, "count_nonnull": 230, "ordered_min": 92, "filtered_ordered_max": 296, "grouped_max": 4},
  "relations_covered": 87,
  "scan_limit": 10000,
  "skips": 1829,
  "elapsed_seconds": 1195
}
```

Two cases were added by hand on 2026-10-12, after the index work that made them answerable at all:
`ordered_max` and `ordered_min` on `usaspending.entity_awards.total_obligation` (191M rows), derived
through the live API and asserted against psql. The file is 1012 cases; the run block above records
the derivation that produced the other 1010.

## Measured

Slices are run with `eval/truth/run_suite.py` against the live model; each case runs the real MCP
tools. "Not measured" is a harness ReadTimeout, never a model failure.

| slice | cases | passed | real failures | not measured |
| --- | --- | --- | --- | --- |
| ordered_min | 92 | 92 | 0 | 0 |
| row_count + count_nonnull | 618 | 588 | 13 | 17 |
| filtered_ordered_max + grouped_max | 300 | (running) | | |

Failure dispositions for the counts slice, from the transcripts and reproduction, not inference:

- **A harness defect produced the two worst loops.** `run_suite` fed the model the first 6000
  characters of each tool result, and the result envelope (`row_count`, `truncated`, `warnings`)
  sits after the rows. 228 of 618 cases held a result over the cap; `usaspending.frec_map` re-fetched
  the same first page eight times with its "13464 rows; paging cannot produce an answer" warning cut
  away at character 10,466, and `form990_soi.compensation_officers` six times likewise. The server
  emitted exactly what stops the loop; the runner ate it. Fixed: `trim_for_model` keeps head and
  tail, contracted; the remainder slice and the counts re-run both measure under the fix.
- **One loop was the old filter refusal.** `form990t_details.total_ordinary_gain_loss` spent 8/8
  turns re-sending `column:not.is.null`, refused with "'is' is not an operator" - true and useless.
  The refusal now names the working spelling; the re-run measures whether the loop recovers.
- **Some are upstream degradation.** PGRST003 504s during multi-GB index builds on the same
  database, including for `limit=1` with no filter; the same count answers in 0.5s when calm.
- **Some are model behaviour.** `mv_district_spending.unique_agencies` computed the null count and
  repeated the same call seven times instead of subtracting from the total.
- **6 answer hygiene.** The right number, but the answer does not restate the column
  ("The count is 11... **11** rows"). The check accepts the identifier or prose spelling, and the
  model satisfies it in ~98% of the hundreds of column-bearing cases, so the check discriminates;
  these are held as genuine failures rather than relaxed on six samples.

All 30 failed cases are staged for a re-run under the calm upstream and the fixed harness; the
results land in this table.

## Conventions

- **One grader**: `eval/truth/grade.py`. These are `numeric_aggregate` cases and grade through the
  same `check_numeric` path as the truth corpus.
- **Every value is derived from the database**, through the signed PostgREST endpoint, never
  through `benthic_query`: a number fetched through the server under test inherits every defect the
  case exists to catch. The derivation helpers are imported from `eval/truth/generate_numeric.py`,
  which this corpus does not edit.
- **NULLS LAST**: Postgres puts NULLS FIRST on a descending sort by default, so every maximum is
  derived with `order={column}.desc.nullslast` and every minimum with
  `order={column}.asc.nullslast`, and every case's `derived_from` records the real request,
  including `.nullslast`.
- **Value floor**: no case expects 0 or a value with magnitude under 10. A count of 1 is a real
  answer but not a hard question, and a zero expectation is satisfied by a server that returns
  nothing. The check is on magnitude, not sign - `entity_awards.total_obligation` is signed and its
  true minimum is -3,552,647,982.00, which the grader reads like any other ten-digit number. Every
  skip the floor forces is recorded with the value that tripped it.
- **Tolerances**: counts are exact. Money figures carry `money()` only where the column name
  indicates currency - it contains one of amount, obligat, cost, value, total, balance, spend,
  loan. Every other figure is exact. There is no blanket money().
- **Filters are derived, never typed**: a filtered case takes the first non-null value of its
  column at `order={column}.asc.nullslast`, counts matches with `Prefer: count=exact`, and is
  emitted only when the count is at least 10 and below the relation total.
- **Provenance per case**: every case records `manifest_hash` (the signed manifest its relation was
  read from), `scan_limit`, and `derivation_seconds`, next to `derived_from` and `derived_at`.

## Kinds

| kind | cases |
| --- | --- |
| row_count | 388 |
| count_nonnull | 230 |
| ordered_min | 92 |
| filtered_ordered_max | 296 |
| grouped_max | 4 |

## Skips

| category | count | example |
| --- | --- | --- |
| all_null | 63 | irs_ng.bmf_organizations.ntee_nccs: ntee_nccs is all-null, so there is no value to filter on |
| composite_key | 2 | usaspending.entity_awards: composite key (award_id, award_type); the aggregate verifies one key column |
| degenerate_filter | 17 | irs_ng.bmf_organizations.f990_total_revenue_recent: filter and value column coincide; the maximum would be the filter value itself |
| floor | 545 | irs_ng.bmf_organization_snapshots: 0 rows is below the value floor of 10 |
| no_answer | 183 | irs_ng.bmf_organization_snapshots.id: no numeric minimum: empty, all-null, or unqueryable |
| no_count | 43 | usaspending.agency_by_subtier_and_optionally_toptier: the endpoint reports no exact count |
| no_filter | 44 | irs_ng.bmf_organization_snapshots: no column produced a usable filter (10 <= matches < relation total) |
| no_numeric_column | 5 | usp_cl.committees: no non-key numeric column to sum |
| no_primary_key | 63 | irs_ng.mv_nonprofit_profile: declares no primary key, so coverage cannot be verified |
| not_queryable | 28 | usaspending.vw_financial_accounts_by_program_activity_object_class_download.drv_obligations_undelivered_orders_unpaid: sample probe failed: HTTP 504 |
| precision_guard | 2 | usp_cl.district_offices: winning total has more than 15 significant digits; the float recorded as expected.value would not round-trip to the exact decimal |
| request_failed | 22 | usaspending.all_entities.legal_business_name: ReadTimeout:  |
| sampling_range | 791 | irs_ng.bmf_organizations.id: count 1 of id=eq.1 is outside [10, 2259215); a filter that matches nothing, everything, or under the floor is not emitted |
| unreachable | 21 | irs_ng.bmf_organizations: 2,259,215 rows exceed the complete-scan limit of 10,000 |

The full skip list, one reason per target, is in `eval/coverage/runs/run-20261011T195359.json` (gitignored,
like every other run evidence directory).

## What is not measured here

- **Relations that declare no primary key never get a grouped_max case** - the correct answer to
  an unverifiable aggregate is a refusal, which the truth corpus already grades as a decline.
- **Non-queryable relations are out of scope entirely**: 20 of the 119 signed
  relations declare `queryable: false` and get no case at all.
- **A filtered case only exercises one sample per column.** The filter is the first non-null value
  at ascending order; other values of the same column are not separately counted.
- **Spatial joins are not exercised here.** They go through benthic_rpc, not PostgREST, so a
  numeric corpus cannot reach them.
- **Counts are single figures, not distributions.** Whether the model reaches the figure by
  counting exactly or by reading the server's own count is not distinguished by the grader.
