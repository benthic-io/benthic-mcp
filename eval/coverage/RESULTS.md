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

Slices run with `eval/truth/run_suite.py` against the live model; every case drives the real MCP
tools. "Not measured" is a harness verdict - a ReadTimeout, or an answer the model cut at its own
token cap - never a model failure: re-run a case before calling it failed. The counts slice was
re-measured after the harness, refusal and grader fixes below; the naics verdict comes from its
re-run under the corrected question.

| slice | cases | passed | real failures | not measured |
| --- | --- | --- | --- | --- |
| ordered_min | 92 | 92 | 0 | 0 |
| row_count + count_nonnull | 618 | 615 | 2 | 1 |
| filtered_ordered_max + grouped_max | 300 | 299 | 0 | 1 |
| entity_awards pair | 2 | 2 | 0 | 0 |
| **whole corpus** | **1012** | **1008** | **2** | **2** |

The two real failures are answer hygiene: the right number without the column restated. The model
satisfies that check in ~98% of the hundreds of column-bearing cases, so it discriminates; the
same two cases also failed on their original run with different wording, and they are kept as
failures rather than relaxed on a handful of samples. The two not measured: one read over a
geographic relation that timed out and then had its answer cut, and one answer cut at the model's
turn token cap - both attempted twice, neither produced a complete answer.

## What measuring found, and what each finding cost

Every failure below was dispositioned by pulling the transcript and reproducing, never by guessing;
the early hypothesis "the model counts pages instead of the table" died on exactly that
reproduction and is kept here as part of the record.

1. **A harness defect manufactured the two worst loops (fixed in `0171774`).** `run_suite` fed the
   model the first 6000 characters of each tool result, and the envelope (`row_count`, `truncated`,
   `warnings`) sits after the rows. 228 of 618 cases held a result over the cap; `usaspending.frec_map`
   re-fetched the same first page eight times with its "13464 rows; paging cannot produce an answer"
   warning cut away at character 10,466, and `form990_soi.compensation_officers` six times likewise.
   The trim now keeps head and tail; four contracts pin it.
2. **One loop was the old filter refusal (fixed in `55b0b60`).** `form990t_details.total_ordinary_gain_loss`
   spent 8/8 turns re-sending the null test with the words reversed; the refusal never named the
   working form. It now does, and the case passes.
3. **The grader could not read scientific notation (fixed).** `all_entities.prime_subaward_amount` has a
   true maximum of `1.0000000001534684e+18`; the model answered it exactly, and the parser read the
   mantissa and the exponent as two numbers. An instrument that fails the answer its own tool result
   taught the model to write is the one failure a grader may not have.
4. **The naics NULL case's question was ambiguous (fixed).** The sampled value is the literal text
   "NULL" (171 rows); the SQL null count is 232, and an unquoted "exactly NULL?" cannot say which
   reading it means. The value and derivation were always right; the display now quotes a value that
   reads as SQL null, the committed question is corrected, and the model reads the quoted form
   correctly - all three tail cases pass.
5. **Upstream 504s are real but transient.** PGRST003 "Timed out" for minutes during multi-GB index
   builds on the same database, including for `limit=1` unfiltered; the same counts answer in 0.5s
   when calm. The re-run recovered them.

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
