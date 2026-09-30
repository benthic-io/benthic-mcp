# benthic-mcp: working memory

## The goal

A working MCP: accurate answers to complex questions, improving as new datasets arrive. A
self-improving guidance loop was the original goal. The evidence says that specific loop cannot work;
what replaced it is below. The mechanism that does work is contracts plus a reviewer, plus an
observer that watches real use and turns recurring stumbles into standing guidance.

## Current state (2026-09-30, end of session 2)

**Repo** `github.com/benthic-io/benthic-mcp`, public, MIT. HEAD `a1882c5`, CI green, **460 tests**.

**Running on 192.168.10.222**, all under systemd user units:
- `llama-server.service` - 8081, `-np 2`, spawns the MCP as a **stdio child**; restart it to make
  the chat path pick up new code
- `benthic-mcp.service` - 8082 HTTP
- `benthic-observe.timer` - one observation cycle every 30 min, `Type=oneshot` + `OnUnitInactiveSec`

MCP config `~/.config/llama.cpp/mcp-servers.json`; env `~/.config/benthic-mcp/env`.

**Scripts:** `scripts/deploy.sh` (push-target-independent deploy: daemon-reload, restart both
services, health check, canary, roll back on red), `scripts/tick.sh` (one observe cycle, with a
`flock`), `scripts/observe-once.sh` (what the timer runs).

**Observer:** `eval/observer/sweep.py` drives probes through the same loop the web UI implements
and records every turn **including reasoning**. `eval/observer/findings.py` reads the **server's own
refusals** - not the model's prose - pairs each refused call with its error, and checks every
identifier against the live signed manifest. 146 recorded case-runs over 11 cycles.

## Resuming an agent automatically

**Background shell commands notify on completion.** That is the only thing that starts a turn
without a human. `/tmp/opencode/wait_for_tick.sh` blocks until a sweep writes a new record file and
exits, which resumes the session. Watch record files, not logs - a log written by a *later* cycle
never contains your marker.

## How to drive the MCP by hand

```bash
curl -s http://192.168.10.222:8081/tools            # 6 MCP tools, namespaced benthic_benthic_*
curl -s http://192.168.10.222:8081/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"local","messages":[...],"tools":[...],"max_tokens":4000}'
curl -s -X POST http://192.168.10.222:8081/tools -H 'Content-Type: application/json' \
  -d '{"tool":"benthic_benthic_query","params":{...}}'
```

llama-server has **no agent loop**: it emits `tool_calls` and stops. The caller executes them and
feeds results back as `tool` messages. The server runs `--reasoning-preserve`, so thinking turns
put output in **`reasoning_content`** and leave `content` empty - read both or 61% of turns look
blank. Tools are not auto-injected; pass them. Results arrive as `plain_text_response`, JSON
encoded as a string. `max_tokens` must budget for thinking: at 1400 one turn spent all of it
reasoning and emitted nothing, which is indistinguishable from a refusal.

## Seven server defects found by driving it, and fixed

| Defect | Was | Now |
|---|---|---|
| `discover` dropped a relation filter | `dataset` + qualified `relation` returned an unrelated relation | returns what was asked for |
| paging with no stable order | 63/99 relations page with no `ORDER BY`; aggregates silently dropped and duplicated rows | refused; single-page still works |
| bare `count(*)` refused | 10,545 rows against a 10,000 limit, and no filter fixes a count | answered exactly from the count HEAD |
| scan-limit refusal | "Narrow the filters", to a caller that had narrowed | names the real count |
| metric/filter rejection | named a shape, never an operator; `name=` vs `alias=` | operator list, worked example, case-insensitive |
| operators case-sensitive | message showed lowercase, parser demanded it | both cases accepted |
| truncated result had no size | `truncated: true` only; model looped | names rows returned of rows matching, and says what to do |

The last one mattered most. The stop rule was **already** in the `benthic_discover` description on
every turn and did not help, because a model cannot budget turns without knowing the size of what
it holds. The server had just paid for a HEAD to learn it and discarded the number on the path that
succeeded.

Measured over 18 identical probes: turn-exhaustion 7/18 -> 4/18. Pass rate is **not** a reliable
signal - six probes flip between runs on identical input.

## The three probes that never answer are not server bugs

`join_partial` 0/8, `query_group_by` 0/8, `self_report` 0/7. The model's reasoning is correct:

> The join on UEI is too wide because those UEIs match 2.6M rows.

They ask for answers this API cannot produce: 1.4M entities joined to 2.6M registrations, 866K
rows grouped, 10M+ awards summed. **Retire or rewrite them.** Making the server more permissive to
satisfy them means shipping an expensive operation as if it were cheap.

Aggregates are disabled server-side: `select=state,count()` returns PGRST123, and bare
`select=count` counts rows, not distinct values. A bounded walk of the sorted group key cannot
establish output size either - measured: 5,000 ordered rows on a 17.9M-row table cover 38 distinct
states and the first 1,000 rows are 60% one value. **The refusal is correct.** The fix was naming
the aggregate-free relations the model was never told about: `mv_district_spending` (16,401),
`state_data` (448), `overall_totals` (141), `vw_published_dabs_toptier_agency` (111).

## False greens: the recurring failure mode

Seven cases of an instrument reporting success while measuring nothing:
- 6 contracts arrived violated and were silently green
- the scorer checked the route, not the result; 55 stored runs rescored red
- a mutation experiment reported 12/12 because the mutation never loaded (symlinked venv, plus a
  syntax error nothing caught because nothing imported it)
- `promote_candidate` printed `served_lessons: 0` while serving 12
- the observer's sweep died on `ModuleNotFoundError` and the tick printed "probe sweep finished"
- `grep -c` on single-line JSON answered 1 for any payload, so a healthy server read as "only 1
  MCP tool registered"
- `NEXT: -` on the timer meant "running", where the previous design's meant "broken"

**Check the instrument before believing the number.** Health checks read `ActiveState`, never `NEXT`.

## Rules learned the hard way

- A contract must fail before the fix. Three of mine shipped green because they asserted the wrong
  thing - `screen_prose` returns what it **kept**, I named it `dropped`.
- A test reading `eval/harness/cache` fails in CI; that directory is gitignored. Contracts must be
  self-contained.
- `pkill` on the MCP binary kills the stdio child and llama-server does not always respawn it. Use
  systemd. Restart `llama-server` to make the chat path pick up code; restarting `benthic-mcp` does
  not.
- `systemctl --user daemon-reload` before restarting, or systemd reuses its cached command line and
  a unit edit silently does nothing. This hid a `--ctx-size`/`-np` change for a full restart.
- The verifier strips guidance naming a relation the manifest lacks, a column that does not exist,
  or an **unsigned join**. Guidance that tells the model to do something the trust boundary forbids
  is worse than none - the first version of the `usp_cl.legislators` guidance said "join on
  bioguide_id" and no signed edge touches `legislators`.
- Backticking a parameter name (`group_by`) makes the prose screener read it as a column reference.
- `pgrep -f` matches the shell running the command; use a self-excluding pattern.
- The model is confidently wrong about the catalog. It reasoned toptier_code `020` was the
  Department of Veterans Affairs; it is the **Department of the Treasury**. Every candidate finding
  is checked against the manifest before it becomes guidance.

## Production access, and the 32-minute orphan

The upstream is not a third party. `benthic.io` (165.22.33.22) is nginx proxying to **thunkah**
(192.168.10.202), which runs the production Postgres. I spent a while guessing wrong here; the
architecture is not derivable from the code.

```bash
ssh -i ~/.ssh/id_ed25519_thunkah thunkah                      # key installed 2026-09-30
PGOPTIONS='-c statement_timeout=15000' psql -d usaspending_db -c "..."
```

`otherdrums` has a Postgres role on thunkah, so **no sudo is needed**. Databases: `usaspending_db`
(the big one), plus `benthic_fresh_check`, `benthic_metrics`, `irs_ng`, `sam_er`, and nine
`benthic_metrics_test_*` scratch databases nobody cleans up.

**The orphan.** A probe call at 16:53:29 was abandoned by nginx at its 60s `proxy_read_timeout` while
Postgres kept executing. It ran **32 minutes** before I found and terminated it - matching the
timestamp to the second. It was `ORDER BY award_id LIMIT/OFFSET` over `prime_awards`, and the plan
explains everything:

```
Limit (cost=99723..100831)
  -> Index Scan using idx_prime_awards_award_id (cost=0.57..112744059 rows=10175169)
       Filter: (fiscal_year = 2023)
```

`prime_awards` is 183M rows / **192 GB** with ~24 GB of indexes. It has single-column indexes on
both `award_id` and `fiscal_year` but **not the composite**, so the planner walks the entire
`award_id` index filtering on fiscal year. 112M cost units. `CREATE INDEX CONCURRENTLY
(fiscal_year, award_id)` turns it into a seek.

**I was wrong twice here and it cost time.** I asserted twice that no index could help these
queries. The index was missing and composite. I had been reasoning from client-side traces without
having looked at the database at all. Lesson: when a question is about performance, go and read the
plan before theorising - two confident wrong answers is worse than one "I don't know".

Related, and the same mistake: `count(*)` here uses a *partial* index
(`idx_prime_awards_pop_state`, parallel index-only scan, cost 1.5M), not a seq scan. Counts are
cheaper than I implied. Only bare counts over whole 400M+ row tables are genuinely expensive.

## What the manifest already knows and the server ignores

`row_count_estimate` is in the signed catalog, **but only for 15 of 119 relations.** Missing from
every relation that hurt: `prime_awards`, `entity_awards`, `all_entities`, `mv_district_spending`,
`state_data`, `overall_totals`.

And the client **parses it and never uses it** - `catalog.py:82,233,377` and `models.py:179` carry it;
it appears in no decision in `query.py`, `postgrest.py` or `joins.py`. The server had a signed
authoritative answer to "is this too big to page?" in memory and sent a 192 GB query to find out
empirically.

The upstream cause is a truthiness bug at `bdp/tools/introspect.py:272`: `if reltuples:` drops the
count when it is `0` and cannot tell "empty table" from "never analyzed". The thunkah agent is
fixing that; I own the client side.

Also: `prime_awards`, `entity_awards`, `all_entities` and the mv tables have **never been
ANALYZEd** - no `last_analyze`, no `last_autoanalyze`, ever. Stage `07_analyze` exists and says
"planner has no stats until this runs", so this is a pipeline that did not reach completion, not a
missing feature. The thunkah agent is finding out why.

**Manifest/commit coupling:** `etl_provenance.commit_hash` sits inside the signed payload. `SPEC.md`
is explicit that labelling a hand-built object `derived` "would be lying about what the referenced
commit produces". So a change is not done until it is in the ETL *and* the manifest. Currently the
manifest records `b25eba84` while the ETL head is `3131c62` - discrepancy unverified.

## Handed off to an agent on thunkah

Database and ETL work is now owned by an opencode session on thunkah; the brief is
`~/thunkah-handoff.md` and the transcript `~/benthic-publish-work/run1.jsonl`. Session
`ses_f0bb26b79ffe0JahS4LXf1JLjb`. Check it with `~/benthic-publish-work/status.sh`.

It is building `benthic-io/benthic-publish` (private) - a playbook to take any dataset from raw
download to a fast, correctly signed, served API. The brief carries the measured numbers, the
`CONCURRENTLY` rule, and the instruction not to touch `benthic-mcp`.

**On thunkah, `/usr/local/bin/opencode` is 1.0.76 and has no `--auto`; only
`~/.opencode/bin/opencode` (2.0.20) does. PATH picks the wrong one.** Without `--auto` an
unattended run hangs on the first permission prompt rather than failing. Always set
`PATH=$HOME/.opencode/bin:$PATH`.

## Next steps on this side, in order

1. **Use `row_count_estimate` as a pre-flight.** Refuse locally and instantly when the estimate
   exceeds the scan limit, so a huge relation costs zero upstream queries. Contract: the second
   identical call must refuse with no upstream request at all. Blocked on the manifest fix for
   full coverage, but the 15 relations that have it can be used now - and it is strictly an
   improvement to consult it where present.
   **Open question for the user:** for the 104 relations with no estimate, refuse or proceed as
   today? Proceeding keeps the catalog usable; refusing is safe but strands 87% of it.
2. **Rewrite or retire the three unanswerable probes** (`join_partial` 0/8, `query_group_by` 0/8,
   `self_report` 0/7). They are the direct cause of the upstream load, and they are questions this
   API cannot answer, not server defects.
3. **Check whether `rpc_*` regressed**: `rpc_box` 6/8 and `rpc_point` 7/8, from a previous 8/8.
4. Do not chase the eleven probes that vary run to run. That is the signal already known not to
   trust.

**The observe timer is currently STOPPED** (`systemctl --user start benthic-observe.timer` to
resume). It generates real production load, and running it while diagnosing a load problem is the
wrong order of operations.

## Files worth reading first

- `docs/findings.md` - the research log, including the negative results and the invalid experiments
- `eval/observer/findings.py` - how a candidate finding is extracted and verified
- `scripts/deploy.sh` - the deploy contract, and its rollback
- `scripts/tick.sh` - what an observation cycle does and refuses to do
- `tests/test_contracts_catalog.py` - the property contracts, including seed-vs-manifest checks