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

## Never add an AI trailer to a commit here

The operator does not use Claude, does not associate with Anthropic, and asked for
every `Co-Authored-By: Claude Opus 4.5` and `Assisted-by: Claude Opus 4.5` trailer to be
removed from the history. All 62 commits were rewritten on 2026-09-30
(`filter-branch --msg-filter`) and force-pushed; HEAD moved `d9a4691` -> `311f66f`.
Verified by cloning from GitHub and grepping the fresh clone: 0 matches.

Write commit messages as the operator's work, plainly, and attribute nothing to a
tool. The llama.cpp AGENTS.md rules against writing commit messages apply here by the
operator's own instruction - `mcp-tools` is not llama.cpp, but the habit carried over
and was wrong for this repo.

**Anyone with a clone must `git fetch && git reset --hard`.** Old objects remain on
GitHub's servers until they expire, so the trailers are unreachable by name but not
cryptographically gone. Only the operator can decide whether that matters.

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

## Where the work went, 2026-09-30 evening

**Pushed, in the order the manifest requires:** `ngopen-pipelines` `ea88629`, `bdp` `64b60b2`,
`benthic-site` `de037f3`. The manifest names `ea88629`; verified by cloning from GitHub and
confirming it resolves. `benthic-publish` is private, 7 commits, 9 documents plus `evidence/`.

**Production is fixed.** `CREATE INDEX CONCURRENTLY (fiscal_year, award_id)` on `prime_awards`:
42:33, 5505 MB, in `ssd_1tb` (every other index there is, and `default_tablespace` is empty, so
an unpinned build lands on the wrong filesystem). `Filter:` became `Index Cond:`, Limit cost
1,108.60 -> 91.24. ANALYZE ran on 7 relations. 9 scratch databases dropped after four checks.

**The never-analyzed mystery is solved:** pg_dump does not carry planner statistics, so a relation
transferred by swap arrives with `reltuples = -1` and no `last_analyze`, and nothing downstream
replaces them. The serving database is not run through the stage sequence, so `07_analyze` never
touches it. Fixed at the swap point in `migrate.py`, not by adding an ANALYZE somewhere else.

**Three numbers were wrong before they were measured**, and both thunkah agents caught them
independently:
- "cost 112,744,059 -> 30.84, a factor of ~3.66 million" - written before the build finished. Real:
  12.2x. The 30.84 was never measured.
- "ANALYZE will reveal the estimate was stale" - wrong. The estimate moved 0.7% and cost moved *up*.
  FY2023 genuinely holds ~10.2M rows. I had conflated how an estimate was obtained with whether it
  is right, which is my error from the opposite direction.
- `--db` does not exist; it is `--dbname`. And "76 recovered indexes" is a hardcoded log string; the
  file has 61.

These went into a README table in `benthic-publish` rather than being quietly fixed, because a
playbook that hides its corrections teaches readers to trust the numbers it did not correct.

## `row_count_estimate`, the exact convention

Integer >= 0, per relation. **Present + 0 = genuinely empty. Present + n = advisory estimate. Key
absent = never analyzed, unknown.** No null convention was introduced and none exists - there is no
`null` or `-1` case from a fixed introspector. So my client-side `estimate > 0` guard was defending
against something the fixed introspector cannot produce; it should test for absence instead.
Live DB: 23 of 77 relations carry one, max 446,469,056. Calibrate against the live DB, not the
published manifest, which still holds the sparse pre-fix set.

## The observer, and the ninth false green

The 19:17 tick printed "probe sweep finished" and exited 0. **The record did not exist.** It built a
survey from records that were never written and reported four "new" hallucinated identifiers.

The cause was mine: rewriting the three dead probes, I flattened `core.json` from `{"probes": [...]}`
to a bare list, and `sweep.py` indexes `["probes"]`. My commit of that rewrite looked clean. Three
defects, not one - the first was hidden by the other two: `sweep.py` exited 0 on an unhandled
exception; `tick.sh` printed "finished" regardless of exit code; and a zero exit was never
sufficient evidence, so the record itself is now checked for existence and non-emptiness.

Pyright had been red on `sweep.py` all along (`get()` declared `-> object` while returning parsed
JSON, so indexing its result was an error at line 57) and the sweep had been running anyway.

**First two full cycles under the new probes: 14/18 answered, both times, with zero production load.**
No query ran longer than 10s for the entire sweep. The three rewrites work; they were the entire
cause of the upstream load.

### The token budget has now produced three apparent failures

At 1,400 a turn emitted nothing. At 4,000 `self_report` spent the whole budget on visible
deliberation and was cut mid-string inside a tool call - `'{"question":"...","source":"'` never
reached the server - so the probe was recorded as not having answered when the model never got to
ask. Default is now 8,000, with 12,000 for the two open-ended probes. The durable pattern: **when
the budget binds, the instrument measures the budget, not the model, and nothing in the output
distinguishes the two.**

### The four that still do not answer, and why

`query_having_text` 0/2, `truncation` 0/2, `join_partial` 0/2, `self_report` 0/2. These are a
different class from the three I retired: not refusals, but **turn exhaustion with usable data in
hand** at 8 turns. There is no agency-level obligation rollup in the catalog -
`mv_entity_spending_summary` has one but times out even on a HEAD count and is deliberately not
offered as a route. So `query_having_text` needs a scan it cannot get, and the other three are
plausible but need more turns than the sweep allows.

Worth deciding rather than assuming: raise max-turns for these, rewrite them to be narrower, or
retire them. Do not quietly let 14/18 stand as "the number" without saying which four and why.

## The publish procedure already existed: `pull_site.sh`

**Do not build a publishing mechanism for benthic.io before looking for one.**
The web server (`165.22.33.22`, `proxy-etc-0`) has its own git checkout at
`/var/www/benthic.io/site`, and the operator's procedure is four lines in `~/pull_site.sh`:

```bash
cd /var/www/benthic.io/site
sudo git pull origin master
sudo /usr/local/bin/hugo
sudo rm -rf /var/www/benthic.io/public
sudo mv /var/www/benthic.io/site/public /var/www/benthic.io/public
```

I spent hours staging a build and writing an atomic-swap script for this, and told the
operator to run a command that had never been copied to the host it was meant to run on.
Two agents independently reported "there is no deploy automation at all" - neither of us
had listed the home directory. Both `pull_site.sh` and my staged tree were in the same
folder. Look for the existing procedure first; it will be adjacent to where you were about
to put yours.

`rm -rf` then `mv` is not atomic, so there is a window where `/bdp/*` 404s. Both paths are
on the same device (64513, xfs) so `mv` is a rename and the window is sub-millisecond,
and there is no backup - the one thing worth adding.

**Result: live collection went 6 -> 9 signed edges.** The new hop is
`usp_cl.legislators.bioguide_id -> legislator_terms` (identifier/reliable) and two
`geom_point -> congressional_districts` spatial edges are restored, the latter having been
collateral damage from a schema migration in commit `2e53270` that renamed relation names
and deleted two edges instead of renaming them.

`check-bdp-drift.sh` on thunkah compares the served collection against the committed one
and exits 1 on drift. It is on a systemd timer every 16 minutes and is how the three
missed releases would have been caught.

## Two MCP instances, and restarting one of them changes nothing

`benthic-mcp.service` (HTTP 8082) and the stdio child of `llama-server` (reached through
`POST 8081/tools`) are separate processes with separate lifetimes. **The 8081 child holds
its own in-memory catalog snapshot and does not restart when `benthic-mcp.service` does.**

After the collection was published with 9 edges, the HTTP service served all of them and
8081 still served 6 - not a bug, just a child that had been up 12h56m. Every check I ran
went through 8081, so I spent a long time convinced the load path was broken. It was not:
`verified-catalog.json` had 9 joins, `Catalog(snapshot).joins` had 9, and only the
long-lived child disagreed.

**When the signed catalog changes, restart `llama-server`, not just `benthic-mcp`.**

Before concluding a change did not take effect, ask which process answered:

```bash
# the HTTP service, reads the cache at call time
curl -s http://127.0.0.1:8082/mcp ...
# llama-server's stdio child, long-lived, holds its own snapshot
curl -s -X POST http://192.168.10.222:8081/tools ...
```

The catalog cache has a 900s TTL and refreshes on its own. I confirmed 9 edges on disk
while the live process still reported 6, and the two converged only after a restart.

## llama-server ran out of VRAM; ncmoe 31 fixed it, and the fix is measurable

Three wedges on 2026-10-01 and 2026-10-04, all identical, all fixed by a restart. Root cause
found by the operator: **`-ncmoe 30` left too little VRAM.** Raised to **31**, and
`--spec-draft-n-max` lowered 2 -> 1 at the same time.

**Measured creep, the falsifiable form of the claim:** 383 MiB free at 22:16, 333 MiB at 06:38
eight hours later. **~6 MiB/hour**, so ~55 hours of margin. The prediction to hold me to: if a
buildup ever exceeds ~400 MB it wedges again. `tick.sh` now logs VRAM every cycle from
`/sys/class/drm/card*/device/mem_info_vram_{total,used}` because `/health` answers ok through a
wedge and cannot see this coming.

Facts that were wrong while I was diagnosing it, kept because they will mislead again if not:

- **This is an AMD box, ROCm gfx906, not CUDA.** `nvidia-smi` failing is expected, not a driver
  fault. `rocm-smi` and `amd-smi` are both installed at `/usr/bin`. I called it "no GPU present"
  and consequently claimed "GPU idle" having never measured GPU utilisation at all.
- **The journal had 3,474,957 lines going back to 2026-08-06.** The claim that no stderr existed
  at wedge time was false; every occurrence is recoverable, including `print_timing` per task.
- **The lost-wakeup theory was wrong.** It fit the evidence I had and none of the rest.
- Every wedge was pre-**admission**: zero `print_timing` lines during the entire wedged period.
  That observation stands and is what pointed at the resource, not the queue.

Five coredumps exist (SIGABRT), all from `TimeoutStopFailureMode=abort` during shutdown, not from
crashes. `gdb` is not installed, so their thread backtraces are unread. If this ever needs
reopening: `sudo dnf install gdb` then `coredumpctl debug <pid>`.

## Status at 2026-10-05, end of session 4

**The combination suite finally ran clean: 87/100 answered.** The previous run's numbers were
void - 58 of its 100 cases had run against a wedged server.

| tag | n | answered | previous (wedged) |
|---|---|---|---|
| grow | 79 | 67 (85%) | 77% |
| now | 14 | 13 (93%) | 86% |
| refuse | 7 | 7 | 2/7 |

`broken_calls: 0` across all 100. One hallucinated relation (`usp_cl.d`, a truncation artifact).
Run: `eval/combination/run-20261004T2211`, at `--max-turns 12`.

**A third of the observer's recorded history was the server not answering.** `is_invalid()` in
`findings.py` derives it (`no turns` and elapsed at the timeout) rather than reading a field,
because 404 of the existing records predate the field. Effect on every rate ever reported:

| | including outages | excluding |
|---|---|---|
| overall | 47% | **72%** |
| disc_qualified | 58% | 100% |
| disc_typo | 57% | 100% |

`findings.py` now **refuses to write findings at all** (exit 2) when the newest cycle contains an
invalid case, and `tick.sh` honours that instead of printing "survey written" regardless.

**The 13 failures are 8 correct refusals and 6 real defects (1 overlap).** The defect is a single
behaviour: `sam-naics`, `geo-district-split`, `p527-me01`, `time-four-clocks`,
`prog-aln-subsection`, `agg-ein-by-state` called only `discover` and `playbook` and never issued a
single `query`. Five show reasoning blocks of 30,663-33,157 chars - the 8,000-token ceiling.

**The token budget cannot be fixed by raising it.** Ladder on `time-four-clocks`:
8,000/300s truncates, 16,000/900s still truncates, 24,000/300s `TimeoutError`, 32,000/1200s
`TimeoutError` at 1,215s. The two knobs are coupled: at ~36 t/s, anything above ~10,800 tokens
outruns a 300s request timeout.

Then tested properly on **four** cases (`run-budget24`), with the decision rule fixed before the
run: >=3 of 4 answering would justify raising it suite-wide. Result: **truncation fixed in 4 of 4,
answered in 0 of 4, at 2.7x the cost** (1,517s -> 4,044s). One case (`geo-district-split`) started
querying and still did not answer. More tokens buy more deliberation, not action. `classify.py`
keeps `--max-tokens` (default 8000) so the experiment is repeatable, but the suite default must
stay at 8,000 - raising it is a 2.7x cost for zero gain.

Four things I got wrong in this session, all the same shape:

1. **`length_cut` is not the main problem** - 23 cases hit it and 16 still answered (70% vs 92%).
2. **The 8 missing signed hops block zero cases** - all 8 answered, naming the gap and giving what
   they could. `missing_hops` in `summary.json` is a wish list, not a defect list.
3. **`refusal_shape.py`'s 4-of-7 is not 4 violations.** Its docstring already documents that
   refining the regex punishes the best refusals; only the *summary wording* asserted a verdict,
   and that is now hedged. Detection deliberately unchanged.
4. **34% of observer history was an outage**, which I had been reporting as model quality.

## Still open

1. **Six cases never issue a `query`, and a raised token budget does not fix it** (0 of 4 on
   `run-budget24`). This is the single remaining behavioural defect and it is a *convergence*
   problem: the model deliberates until it is cut off rather than deciding it cannot proceed.
   `BASE_CORE` has six always-on rules and none says what to do when you cannot proceed - rule 2
   says "never invent a join" but not "and if no signed path exists, stop and report". Adding one
   is not free: `_MAX_CORE_LINES = 9`, and the cap leaves room for "exactly one further rule that
   earned its place by measurement". It needs a contract and a measurement, and per playbook.py's
   own note, twelve grounded lessons were once measured as worth no more than one hand-written
   rule - so grounding a true statement is not evidence that it changes behaviour.
2. **`classify.py` records tool names with no per-call durations**, so I cannot tell a slow query
   from slow thinking. 43 of 100 cases exceeded 295s and that gap is still unmeasured.
3. **`migration_status: "migrated"` is now false**, not merely stale - three live indexes sit
   outside version control. `derived` vs `recovered` is a provenance claim and should be a human's
   call.
4. **Tighten the `estimate > 0` guard to test for absence**, per the convention above.
5. **Five files are modified and uncommitted**: the observer invalid-case guard (4 files) and the
   `classify.py --max-tokens` flag. No commit has been made; per AGENTS.md that needs explicit
   approval and the message is the user's to write.
6. **The observe timer is stopped**, deliberately, so the suite had uncontended model slots. Restore
   it when the manual runs are done.
7. `eval/run_eval.py:686` does `questions["metadata"]` and the questions file has no such key, so
   the canary dies with `KeyError` when it runs. `tick.sh` never invokes it, so this looks vestigial.

## Files worth reading first

- `docs/findings.md` - the research log, including the negative results and the invalid experiments
- `eval/observer/findings.py` - how a candidate finding is extracted and verified
- `scripts/deploy.sh` - the deploy contract, and its rollback
- `scripts/tick.sh` - what an observation cycle does and refuses to do
- `tests/test_contracts_catalog.py` - the property contracts, including seed-vs-manifest checks