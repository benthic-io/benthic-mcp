# benthic-mcp: working memory

## The goal

A working MCP: accurate answers to complex questions, improving as new datasets arrive. A
self-improving guidance loop was the original goal. The evidence says that specific loop cannot work;
what replaced it is below. The mechanism that does work is contracts plus a reviewer, plus an
observer that watches real use and turns recurring stumbles into standing guidance.

## The deployment

Durable facts about where this runs. Volatile numbers deliberately left out; the current ones are in
the status section near the end.

**Repo** `github.com/benthic-io/benthic-mcp`, public, MIT, CI green.

**Running on 192.168.10.222**, all under systemd user units:

- `llama-server.service` - 8081, `-np 2`, spawns the MCP as a **stdio child**; restart it to make the
  chat path pick up new code. This is an AMD box on ROCm gfx906, so `nvidia-smi` is not the tool -
  `rocm-smi` and `amd-smi` are.
- `benthic-mcp.service` - 8082 HTTP
- `benthic-observe.timer` - one cycle every 30 min, `Type=oneshot` + `OnUnitInactiveSec`

MCP config `~/.config/llama.cpp/mcp-servers.json`; env `~/.config/benthic-mcp/env`; audit output
`$BENTHIC_AUDIT_DIR`, default `/tmp/opencode/audit`.

**The observation chain, which is the part worth knowing:** `benthic-observe.timer` runs
`scripts/observe-once.sh`, which calls `scripts/tick.sh --probe`, which runs the contracts, then
`eval/observer/sweep.py`, then `eval/observer/findings.py`. `observe-once.sh` exists so the systemd
timer is the scheduler; `tick.sh` is the cycle itself. Neither is redundant and neither is called from
the other directly by systemd.

**Deploy** is `scripts/deploy.sh`, invoked by hand: daemon-reload, restart both services, health
check, freshness check, canary via `eval/run_eval.py --questions eval/canary/questions.json`, roll
back on red.

**Freshness is now checked, because nothing noticed a three-day-old service.** `benthic-mcp.service`
had been running since 2026-10-02 while `src/` moved underneath it, and the health check could not
see it: it restarted both services but only probed 8081, so a dead or stale 8082 still went green.
`freshness_check` compares each unit's start time against the newest mtime under `src/` and fails the
deploy on a difference. It uses **file mtime, not commit time** - a commit is recorded when it is
written, so comparing against it would report a correct deploy of fresh code as stale. 8082's
endpoints all require the bearer token, so its liveness is asked of systemd rather than over HTTP.

`tick.sh` logs the same comparison every cycle but does not refuse on it: the subject of a cycle is
8081, and a stale 8082 is worth knowing about without stopping observation of a healthy 8081.

**Observer:** `eval/observer/sweep.py` drives probes through the same loop the web UI implements and
records every turn **including reasoning**. `eval/observer/findings.py` reads the **server's own
refusals** - not the model's prose - pairs each refused call with its error, checks every identifier
against the live signed manifest, and refuses to write findings at all if the newest cycle contains a
case the server never answered.

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

**A gate that stops at lint is not a gate.** Five commits went red on `main` at CI's format step
while I reported "gates green" after every one. The local sequence was `ruff check`, and `E501` is in
`per-file-ignores` for `eval/*.py`, so lint passes over a 129-character signature that `ruff format`
then wraps. The violation entered at `6d7fd47`.

CI runs, in this order, and all four are required:

```sh
uv run ruff check .
uv run ruff format --check .     # the one that was missing
uv run pyright
uv run pytest -m "not live" -q
```

Note that pyright here covers `src` and `tests` only, per `[tool.pyright] include` - not `eval/`.
Widen it deliberately if you want eval type-checked, but do not assume CI is checking it.

The same failure has a longer history here, all of it an instrument reporting on something other than
what was asked:

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

It happened again, so this is twice, not once. On 2026-10-05 eleven commits were
found carrying `Co-Authored-By: none` - written after the first scrub, by the same
work that added this rule. Rewritten with the same operation and one force-push
(`f15ed71` -> `98faf2f`, 79 commits). Verified by fresh clone: 0 anchored trailer
lines.

The lesson is not about the wording. A rule written down is not a rule followed,
because the check has to be mechanical: the commit stating the rule was written by
the same hand that broke it eleven times. Verify with a clone and a grep, never by
reading the message you have just written.

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
- **A contract passing before you wrote the fix is a warning, not a relief.** The `order=` contract
  built a `QueryRequest` by hand with the order already on the `RelationSource`, so it passed against
  unfixed code by bypassing `build_single_query` - the boundary the tool actually crosses. Same shape as
  the source-alias defect above: build the test through the path the caller uses.
- **Read the field name in the code you are copying.** `sweep.py` reads `message["reasoning_content"]`
  for a model's reasoning, and its comment explains why: this server runs `--reasoning-preserve`, so
  `content` is empty on a thinking turn, and recording only that makes 61% of turns look blank. A new
  runner read `content`, so every transcript it produced was missing its reasoning - which the grader
  reads by design. It presented as two empty answers on one capability and read as a model failure
  until the transcripts were opened.
- **Read the contracts you are about to break.** Regenerating `eval/generated/questions.json` to
  refresh stale sampled values silently reverted three deliberate decisions made on evidence.
  `tests/test_canary.py` caught it - it asserts canary cases are byte-identical to their generated
  counterparts - and I had not read that contract.
- **Read what a check flagged rather than reasoning about what it should flag.** Six false positives in
  one grader, every one found by opening the flagged item: filter syntax, column aliases and an acronym
  read as invented relations, and a model correctly rejecting a trap it had named by mistake.
- **A rising number is not progress.** Every improvement came from checking a claim against something
  independent - `psql`, a contract that had to fail first, a corrupted expectation that had to grade
  wrong. The grader had six defects before it found one real thing.
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

**A second session works on thunkah and is doing index and ETL work, as of 2026-10-09.** It runs
`EXPLAIN ANALYZE` against these databases and, when it adds an index, also modifies the ETL pipeline
so a from-scratch run still produces the current database state. Two consequences for work here:

- **A slow or apparently frozen query may be its index build or its `ANALYZE`, not a defect to work
  around.** Before raising a timeout, cutting a query short, or recording something as slow, check
  `pg_stat_activity` for a running index build or `ANALYZE`. Two `ANALYZE`s cannot run concurrently
  on one table, and both sessions then queue behind each other.
- **It is the pipeline's owner, so this is a conversation, not an edit.** An index added out of band
  would leave the ETL unable to reproduce the database, which is exactly what
  `check_pipeline_provenance.py` exists to catch. Report the query and let that session own the fix.

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

**Manifest/commit coupling:** `SPEC.md` is explicit that labelling a hand-built object `derived`
"would be lying about what the referenced commit produces", so a change is not done until it is in
the ETL *and* the manifest. The hash pair recorded here earlier (`b25eba84` against `3131c62`) was
stale and is now measured properly - see "The pipeline is accurate; the manifests do not say so".

**A ninth false green, and the biggest one.** The `CREATE INDEX CONCURRENTLY` on `prime_awards` that
fixed a 42-minute query went straight to production and into the pipeline file weeks later. That
divergence is what made "is `migration_status` false" a three-day question, and the divergence sat
undetected for three weeks because nothing compared a manifest pin against the code on the runner.

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

`check-bdp-drift.sh` on thunkah compares the served *collection* against the committed one
and exits 1 on drift. It is on a systemd timer every 16 minutes and is how the three
missed releases would have been caught. It compares the join graph only - not dataset manifests,
not schema - so the dataset-pin gap below passed straight under it. The script also lives in
`$HOME` unversioned, which is the ownership gap its own header complains about;
`scripts/check_pipeline_provenance.py` lives in benthic-mcp instead and thunkah runs it from a
clone at `~/benthic-mcp-checkout`.

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

**The creep stopped.** All 22 logged readings, 2026-10-05 18:20 to 2026-10-06 23:07: free VRAM goes
**330 -> 339 MiB over 28.8 hours, a net gain of 9 MiB.** The last five deltas are -3, -2, -2, 0, so it
is flat. The "~4.2 MiB/h, ~63 hours to zero" projection was taken from a short window and no longer
describes anything.

Read the series in cycle order rather than by grepping one log file, because individual cycles show
swings of +221 and -282 MiB that net out and are not creep: reading a single file makes the trend look
like whatever that cycle caught. The per-cycle start time is in the filename
`logs/observe-<YYYYMMDD>T<HHMMSS>.log`, and `tick.sh` prints `vram` as its fourth line.

**339 MiB free, above the ~300 MiB trigger, so no restart is warranted.** Do not restart on the older
projection; read the series. `nvidia-smi` failing here is expected (AMD/ROCm, see below) and `free -m`
showing 716 MiB available is host RAM, not VRAM - I read that as alarming before noticing what it was.

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

**The ignored run evidence is not disposable, and `.gitignore` no longer says it is.** `findings.py`
sums `answered`/`run` across *every* record ever written (`findings.py:211`), so the per-probe rates in
`findings.json` have the whole history as their denominator; `trend.py` walks every directory under
`eval/runs`. Pruning either moves numbers silently rather than tidying. The blanket "regenerated by a
command in the docs" line also covered `grok/`, which no command here recreates. Both claims were
measured before being corrected - 97 records, 29MB, 7 days - and the comment now says re-running
repopulates a path rather than reproducing what is in it. Two contracts in
`tests/test_loop_invariants.py` hold the rules; the first was checked to fail with `grok/` unignored.

Quantified by running `findings.py` both ways: dropping 4 of the 97 files moved `invalid_total` from
404 to 360 and the denominator on 11 of 18 probes, flattering every rate it touched. Those 4 are
ranks 2, 3, 4 and 5 by size - the smallest records in the set - and each holds 11 cases of which
**all 11 are invalid**, because they are cycles where the server answered nobody. **The tidiest-looking
files are the ones carrying the evidence that the server used to fail**, so size is inversely related to
what is worth keeping here and any "delete the small stale files" sweep starts by deleting the evidence.

**Slow cases are slow thinking, not slow server.** `classify.py` now records per-call timings and a
run-level split. On `time-health`: 458.7s total, **30.9s in tools (6.4%), 427.8s in the model (93.6%)**,
slowest single call 30.2s, four of six calls returned in 0.08s. So server latency is not where the
remaining failures live, and shaving query time would not touch the six stall cases.

Four things I got wrong in this session, all the same shape:

1. **`length_cut` is not the main problem** - 23 cases hit it and 16 still answered (70% vs 92%).
2. **The 8 missing signed hops block zero cases** - all 8 answered, naming the gap and giving what
   they could. `missing_hops` in `summary.json` is a wish list, not a defect list.
3. **`refusal_shape.py`'s 4-of-7 is not 4 violations.** Its docstring already documents that
   refining the regex punishes the best refusals; only the *summary wording* asserted a verdict,
   and that is now hedged. Detection deliberately unchanged.
4. **34% of observer history was an outage**, which I had been reporting as model quality.

## The pipeline is accurate; the manifests do not say so

**`scripts/check_pipeline_provenance.py`**, run on thunkah by `pipeline-provenance-check.timer`
every 30 minutes, exit 1 on drift, exit 2 when it could not reach its subject. Read-only. The logic
is in benthic-mcp so it is version controlled; thunkah runs it from `~/benthic-mcp-checkout`.

**Pin drift, all five datasets.** The runner's checkout is `991a8715` (2026-10-03, "Drop six
duplicate indexes"). Every manifest pins older: `usaspending` `ea88629f` (09-30), the other four
`66f58556` (08-11). **The pipeline that built the database is present on the runner.** Nothing
recorded that it was, because publishing the manifest is a separate step - the same ownership gap
`check-bdp-drift.sh` exists to catch, one level down.

Proof the stamp is not run-derived rather than merely stale: `usaspending` carries
`migrated_at: 2026-09-21` against a `commit_hash` dated **09-30**. A run cannot stamp a commit that
did not exist. `irs_ng` is consistent (migrated 08-14, commit 08-11), so some pins may be
run-stamped and some not, which is worse than uniformly hand-written because it looks trustworthy.

**Schema drift, two datasets.** `irs_ng` (78), `samer` (25) and `up_cdmaps` (9) are clean.
`usaspending` has three live indexes the pipeline does not declare and twelve it declares that are
absent; `usp_cl` has six declared-but-absent, including `idx_lt_bioguide_id`. Verified absent from
every schema, not merely missing from `public`.

`uei_crosswalk` is the instructive one. The pipeline declares `idx_uei_duns` and `idx_uei_uei`; the
database carries `uei_crosswalk_uei_idx`, `uei_crosswalk_pkey1` and
`uei_crosswalk_awardee_or_recipient_uniqu_idx`. **A rebuild would create the declared names and never
drop the live ones**, producing a different index set than the one it replaced. That is the failure
the whole exercise exists to prevent, and it is invisible to any check that compares names.

**Three instruments in this project were blind before they were useful**, all fixed by having them
report their own coverage: this one first read only `pipelines/<ds>/sql` and called 74 healthy
indexes undeclared when they live in `recovered/<ds>/`; `is_automatic` missed `_pkey1`, so
`uei_crosswalk_pkey1` would have been permanent false drift; and the run ledger in `src/ngopen_bdp`
declares indexes in Python, which this check does not read - stated in its output rather than left
implicit.

**`commit_hash` means "the state of the database is created by this commit."** Operator, 2026-10-07.
This settles the open question above, and it settles it the uncomfortable way: the current pins are
**false by definition**, not merely stale. Indexes were added after `ea88629f` / `66f58556` and
applied outside the pipeline, so a rebuild from a pinned commit does not reproduce the state it claims
to describe.

Consequences, which are not all mine to act on:
- Advancing to `991a8715` would be **honest** for `irs_ng`, `samer`, `up_cdmaps`, whose declared
  indexes are all present and defined identically to the live database.
- Advancing to `991a8715` would **still be false** for `usaspending` and `usp_cl`, where 12 and 6
  declared indexes are absent and `uei_crosswalk` would rebuild under different names.
- Manifests are signed, so re-pinning is the pipeline owner's action, not ours.

Still open, and also not mine: thunkah's checkout is on branch `main` tracking origin, not detached at
a pinned commit, so "the pipeline that ran" is a moving target even though it is clean and in sync.
Detaching at the commit, or having the run stamp the commit it used, is what makes this check's answer
exact rather than approximate.

**This check also compared index names and nothing else, which I described as comparing definitions.**
`pg_get_indexdef` was never queried and tablespace was printed but never compared, so it could not see
the failure it was written for - `CREATE INDEX IF NOT EXISTS` succeeding against a wrong index that
already owns the name. Fixed in `0192eb3`. Four attempts were needed and every one produced phantom
mismatches in the same direction (the pipeline omits the default `USING btree`; predicate parentheses
differ; a prose comment containing the words "CREATE INDEX CONCURRENTLY" parsed as an index *named*
`CONCURRENTLY`; `gist(` against `gist (`). The comparison is now bounded on purpose: the index head -
name, relation, method, column list, uniqueness, whether a predicate exists - is exact, and
predicate-only differences are reported but not counted. Three currently, all `0` against `0::numeric`.
Normalising a predicate means choosing between calling that equal and calling `(b)::text <> ''` and
`b <> ''` equal; the second is a real difference, so the predicate is reported rather than gated on.

## `query_having_text` could not be answered because the route was unfindable (fixed `16bfbd7`)

It failed in **all 35 complete cycles** in the corpus and was never once answered. Not a server defect -
the route works, and after the `order=` fix it returns the right answer:

```
usaspending.reporting_agency_overview, fiscal_year=2025, fiscal_period=12,
group by toptier_code, sum(total_dollars_obligated_gtas), order desc
  -> 075 = 5,588,699,606,177.20      then vw_published_dabs_toptier_agency for the name
```

**The relation was simply not findable.** Of seven natural phrasings of "agency total obligation", six
never surfaced it in `discover`; the `usaspending` guidance named `mv_district_spending` for state and
district totals and said nothing about agency. The fix is a `RelationGuide` whose `terms` are those
phrasings, plus a `when_to_use` line. Verified after restart: all seven now surface it, and for the
probe's exact question it ranks **1st**, with the name lookup 2nd.

**The trap the guide has to carry too.** The relation declares **no primary key**, so an aggregate over
it is refused as unreliable - a whole fiscal year is 1,221 rows and is refused, a year *and* period is
about 102 and works. It also holds one row per agency per period, so "the largest total obligation" has
no single answer until the period is fixed. Both are in the guide; a model told only that the relation
exists would spend its turns finding the limit.

Note `Catalog.relations` is keyed by a **`tuple`** `(dataset, relation)`, not a dotted string - the
playbook's own dict uses dotted strings, so the two look alike and are not.

**The probe's `expect` was rewritten.** It named a text-aggregate comparison TypeError that is fixed and
that the probe never exercised - no cycle in the corpus issues a `having` clause - so a pass would have
been credited with proving a regression guard it never touched. `relation()` in the test factories gained
a `primary_key` override so a fixture can say "no primary key" truthfully; inventing one would have let
guidance about that refusal pass screening while describing a relation the manifest does not contain.

## Two accuracy defects, 2026-10-07

**A refusal that can answer should say so.** `_scan_refusal` in `src/benthic_mcp/query.py` refuses any
aggregate that would need a complete scan past the limit. The refusal used to say only how far over the
cap the request was, so a model asking "how many IRS exempt organisations are on record" - a count, and
nothing else - was refused, and then spent twelve turns enumerating organisation codes by hand while
the answer sat in the message it had already been given. It now says **the count is the answer to the
question as asked, report it as exact**, when the aggregate is a count over a single non-nullable column
with no grouping. For a nullable column it instead says the figure is **an upper bound** and names the
`not.is.null` filter that makes the two agree. `uei` is nullable in the live signed manifest despite
being the primary key, so production correctly takes the upper-bound branch.

**The branch was inert in production for a day, and the tests did not catch it.** Two defects of my
own. `benthic_query` qualifies an aggregate column as `count:s.id`, so the nullability lookup missed
and the branch never ran; and the test factory's `relation()` omitted `nullable`, which
`catalog.py` reads as `column.get("nullable", True)` - so the fixture said nullable where the intent
was non-nullable, inverting what the test was for. **A private-function test passed the whole time.**
The general rule this earns: a test must cross the boundary the caller crosses. Here that meant
building the contract through `QueryService` with the same qualified form the server actually receives,
which is what `tests/test_query.py` does now.

**Verified but not yet shown to change an answer.** The refusal wording is confirmed correct live in
both branches, but that is a message, not a behaviour. The only baseline number is **87/100** from the
clean 2026-10-04 run - and that counts *answered*, not correct, because the combination suite
deliberately does not score. See "'87/100' was never a correctness score" below before quoting it. The observer's most recent complete cycle
(`20261007T200544`) answered **14/17**, failing `deadend_empty` and `query_aggregate` at the full
12-turn budget and `query_having_text` at four - the same three as the cycle before, so these are stable
rather than noise.

**Accuracy is unmeasured, not merely unmeasured-since.** The suite has no expected values by design,
so no run of it can say whether an answer was right. The gap below is about a refusal carrying no
number, which is countable without a grader.

**The largest remaining accuracy gap is refusals with no number in them.** `matched_rows` is set in
`PostgrestTransport.fetch` (`src/benthic_mcp/postgrest.py:36`) and is `None` when the count request did
not answer - already documented in the code as "not an error". Measured across the whole record
corpus: **562 cap refusals, 110 of them (20%) carrying no number at all, and 106 of those 110 on
`usaspending.prime_awards` alone** - 183M rows, 192 GB. So for the highest-volume relation in the
dataset the refusal degrades to "narrow the filters", which is exactly the unhelpful message that was
just fixed. Not yet diagnosed: whether the count is lost to the 30 s client
`request_timeout_seconds` (`config.py:76`), a server-side `statement_timeout`, or PostgREST's own
count cost. **Do not fix it with `reltuples`.** That message claims the number is exact and instructs
the caller to report it as such; an estimate there would replace an unhelpful refusal with a
confident false claim. An estimate is admissible only in the "how far over the cap" framing, labelled.

## The count refusal had no number in it, and the obvious fix was the wrong one

`count_matching` passed `limit=None` into `_build_params`, which **omits the parameter entirely**. The
comment there claimed `Range: 0-0` bounded the request. It does not: **`Range` is honoured on GET and
ignored on HEAD**, so with no limit PostgREST's `page_total` degenerates to `count(*)` over the whole
relation. Captured from `pg_stat_activity`, the request the transport actually sends expands to a
`SELECT count(*) FROM prime_awards` alongside the page.

| usaspending.prime_awards, 183M rows | result |
|---|---|
| the form the transport sent | **825s, 834s** on two runs |
| with `limit=1` | **3.2s**, exact |

Both long runs were killed by **`statement_timeout=30s` on the `api_user` role** - verified directly:
`select rolconfig from pg_roles where rolname='api_user'` returns `{statement_timeout=30s}`.
`count_matching` turns any failure into `None`, which is why 106 of the corpus's cap refusals carried no
number at all.

**Raising `BENTHIC_REQUEST_TIMEOUT_SECONDS` would have changed nothing.** The client timeout and the
server-side `statement_timeout` are both 30s, and the client wins that tie by about 150ms, so the trace
cannot tell them apart - but a 40s client still receives `HTTP 500` with `proxy-status: PostgREST;
error=57014` (`query_canceled`). A longer client timeout only converts a fast `None` into a slow one.

Verified end to end after `c1d7fb7`, through the real transport:

```
benthic_query  metrics=[max:total_obligation]  on usaspending.prime_awards
  -> "182995658 rows match the filters in usaspending.prime_awards, 182985658 more than
      the complete-scan limit of 10000"          3.2s
```

`182995658` is the true `select count(*)`. Previously that same refusal said only "More than 10000 rows
match". This is not a size problem specific to `prime_awards` either: `entity_awards` (191M) failed
identically and `financial_accounts_by_awards` (446M) fails even with `limit=1`, because its bare count
alone is 32.2s. It is a cliff at `statement_timeout`, not a threshold with a safe side.

**A pre-existing contract had to be corrected rather than satisfied.** It asserted the count request
carries no `limit` at all, with the rationale *"a count over 0 rows is not a 0-row request"* - a
statement about `limit=0`, applied to every limit, which forbade the `limit=1` that fixes this. A HEAD
never carries a body, so the absence of a limit was never what avoided the transfer; the
`count_body_reads` assertion in that same test is what checks that, and it still passes. Narrowed to
what it means, with the positive requirement in a new contract.

**Do not solve this with `reltuples`.** The refusal says the number is exact and instructs the caller to
report it as such; an estimate there replaces an unhelpful refusal with a confident false claim.

## `order=`, `limit=` and `offset=` were silently ignored (fixed in `4064a2a`)

**This was a correctness defect, not a tuning problem, and it made the observer's pass rate
meaningless.** `build_single_query` constructed `RelationSource(...)` with only `alias`, `dataset`,
`relation`, `select` and `filters`. `order`, `limit` and `offset` therefore kept their defaults -
`order=[]`, `limit=None`, `offset=0`. `postgrest.py:228` only appends an `order` param when
`source.order` is non-empty, so **PostgREST was never asked to sort**. `_order_rows` sorted the
fetched page locally, and `fetch` fell back to `default_query_limit` of 100.

Before, against ground truth:

```
order=[total_obligation:desc] limit=1, usaspending.prime_awards
    -> MULTIPLE RECIPIENTS  2,698,943.00          <- what the server called the largest
count(total_obligation > 2,698,943)              -> 2,374,098 rows are larger
psql ORDER BY total_obligation DESC LIMIT 1      -> 373,109,113,199.00
```

Off by a factor of ~138,000, with 2.37 million rows larger. `limit` was also capped at 100 whatever
the caller asked for, despite the tool advertising `le=1000`, and `offset` never paged at all.

After, same call, live:

```
order=[total_obligation:desc] limit=1  ->  MULTIPLE RECIPIENTS  373,109,113,199.00   4.5s
```

**What this means for every number measured before it.** 14/17 was not measuring accuracy.
`query_order_mixed` passed while returning five recipients at `$0.00`; `20261004T215351` answered
`query_having_text` with Treasury at $1.43T when the true agency maximum is HHS at $5.59T.
**Expect the pass rate to fall before it rises - that is the correct outcome, and reading it as a
regression would be reading a corrected measurement as a new defect.**

Pushing an order down is only sound for a plain row select. An aggregate alias is computed output and
a grouped query orders over the group, so both keep the local sort, which is correct there because the
database computed them. `fetch_complete`'s primary-key order for stable paging is untouched.

**A contract that passed for the wrong reason, worth remembering.** The first version of the `order`
contract built a `QueryRequest` by hand with the order already on the `RelationSource` - so it passed
against the unfixed code, because it bypassed `build_single_query`, which is the boundary the tool
actually crosses. Same shape as this morning's source-alias defect: the test had to be built through
the same path the caller uses, or it proves nothing about production.

## Two refusal messages advertise a capability they do not have

Both were found by the same review and both are one-line corrections, but they are separate defects
from the `order=` one above and should be fixed separately so each is measurable.

**The `in` filter.** `_FILTER_SYNTAX` (`query.py:589`) tells every caller that
`'column=in."a","b"'` takes a JSON array, and that exact string is then refused by `_parse_filter`
(`query.py:623`), which requires `json.loads` to accept it - and `json.loads('"a","b"')` raises
`Extra data`. Confirmed live: `uei=in.["A","B"]` works, `uei=in."A","B"` does not. The corpus contains
the model **copying the advertised form verbatim and being refused for it**, in `deadend_empty` T6 of
`20261007T200544` and T7 of `20261006T170851`. The message names the one shape that cannot work and
never shows the one that does.

**The truncation warning.** It tells the model the result is "a partial view rather than a total" and
to answer from the rows held. Sound about completeness, actively false about ordering - and
`20261004T215351` followed it exactly ("since we're ordering by obligation descending, the first row IS
the maximum") and was scored as a pass. This warning should state that the ordering is page-local.

The shared cause is that each message was written to be unmissable in isolation and neither was
checked against what the model does next. `deadend_empty` has failed in **33 consecutive** cycles since
2026-10-06 01:17 and `query_having_text` in **all 35** complete cycles in the corpus - neither has a
passing cycle to point at except via a different route.

## "87/100" was never a correctness score, and it is quoted everywhere

`eval/combination/classify.py` **does not score**. Its own module docstring says why:

> "The suite arrives as 100 English questions ... with no expected values. That is deliberate and
> correct: an `expected` written by whoever ran the tool last would describe whatever the server
> happened to do, which is how a suite becomes a rubber stamp. This script therefore does not score.
> It drives every question, keeps the whole transcript including reasoning, and leaves the judgement
> to a reader."

The refusal record is separated from the failure record for the same reason, and the observer reads
refusals from the server's own error text rather than the model's prose, "because prose matching finds
filler". This is the project's best existing discipline and it is why nothing here is self-graded.

**So 87/100 means 87 produced *an* answer.** Correctness is unmeasured and, without expected values,
unmeasurable from that suite. The cases carry `answered`, `tools_called`, `server_refused`, `length_cut`
and `missing_hop` - no grade field, and no judge anywhere in `eval/`.

Two consequences, both of which I got wrong earlier in the session and had to be corrected:

- **It measures coverage, not accuracy.** Every "the MCP is at 87%" style statement, including ones I
  wrote today, overstates what is known.
- **That run predates every fix in this session and `order=` was broken during it**, so an unknown
  share of those 87 answers were confidently wrong - `query_order_mixed` style cases scored
  `answered=True` on a sorted page.

**Read `answered` as "did not refuse and did not run out of turns", never as "was right."** If a real
accuracy number is wanted, it needs expected values written from something other than the server's own
last behaviour, which is a deliberate piece of work and not a threshold tweak.

## Two negative results, 2026-10-08, both from hypotheses that died on measurement

Recorded so nobody re-derives them. Both looked like findings until the follow-up measurement.

**The 19 "new" hallucinated identifiers are real model guesses, not instrument noise.** `findings.py`
extracts identifiers from *refusal text*, so I expected the server's own vocabulary to be leaking in -
`eq` is a filter operator it names in its own syntax text, and `count` is its own suggestion word. Both
are real server text. But the source messages are the model guessing: `chamber` and `fname` against
`usp_cl.legislator_terms`, `awards` and `districts` as relation names. `eq` came from the model writing
`eq.MA` as if it were a column. **`findings.py` is working correctly.**

**The 12-column inline cap costs no turns, so "did you mean" closers would buy nothing.** The refusal
tail already says *"This relation has 22 columns and discovery lists only 12; call discover with
`detail='full'`"*, and the model acts on it: of 97 such refusals, **80 (82%) were followed by exactly
that fetch**. The tempting statistic was that 70 of 130 refusals give no inline suggestion and 70% of
"suggestion-only" refusals are followed by giving up - but that is confounded. Suggestion-only refusals
concentrate in the probes that fail for unrelated reasons (`truncation` and `deadend_empty` burn the
full 12-turn budget and answer 0% of the time), while `join_signed_both` answers 71% *despite* them. The
metric measured the probe's baseline pass rate, not the suggestion's quality.

Net effect: **no fix, and M3's `discover` column budget is safer on this axis than it looked.** The cap
directs the model to a follow-up call that works; tightening it to 6 columns would widen the set of
relations where a guess-then-fetch round trip is needed. The `discover` bound remains a prompt-token
saving with `b5344f5`'s negative result behind it and no accuracy benefit I could demonstrate - which
is why it is last in the queue rather than first.

## Where the probe set actually stands

Per-probe across all cycles, invalid excluded:

```
disc_qualified     91/ 91  100%      query_order_mixed   80/ 81   99%
disc_typo          90/ 90  100%      join_wrong_edge     81/ 84   96%
playbook_need      75/ 78   96%      rpc_out_of_coverage 80/ 83   96%
rpc_box            80/ 83   96%      wrong_column        74/ 78   95%
rpc_point          77/ 83   93%      historical_terms    73/ 80   91%
query_group_by     72/ 81   89%      self_report         59/ 71   83%
join_signed_both   70/ 81   86%      query_aggregate     54/ 83   65%
truncation         30/ 75   40%      deadend_empty       30/ 82   37%
query_having_text  11/ 81   14%      join_partial         4/ 40   10%
```

These are **corpus** figures and mostly predate the fixes. The two cycles after the fixes read 16/17 and
**17/17**, and the three that had been stable failures all broke: `deadend_empty` (33 consecutive
failures) and `query_aggregate` (17 of 19) now answer, and `query_having_text` - which had **0 passes in
35 complete cycles** - passed on the first cycle after the refusal fix.

**`join_partial` at 4/40 is retired and must not be attacked.** It was dropped from the probe set on
2026-09-30 - every one of its 40 runs predates that, and it exists in no current probe file. It only
looked like the worst live probe because I read historical records as current. The lowest *live* rates
are `query_having_text` (14%), `deadend_empty` (37%) and `truncation` (40%), and all three had their
cause fixed today; the two cycles since read 16/17 and 17/17.

`truncation` at 40% is the one I have not explained. It answers in the last two cycles, so whatever it
was has been overtaken, and the truncation-warning fix that would have targeted it was deliberately not
made - that defect was a consequence of the broken `order=`, so there may be nothing left to fix.

## Measuring correctness, and the grader that had to be fixed first

**`eval/truth/grade.py`** grades `eval/generated/questions.json` - 30 cases that have carried
`expected` values since 2026-09-29 with **nothing grading them**. Their expectations come from the
live database at generation time (`generate_cases.py` calls `fetch_rows` and `sample_key`), which is
what makes them gradeable. `classify.py` refuses to score the *other* suite for the opposite reason:
an `expected` written by whoever ran the tool last would describe whatever the server happened to do.

**Grading is per capability and there is deliberately no overall rate.** An aggregate over exact RPCs
and partly heuristic joins is a number that cannot be acted on, and it invites optimising the suite
until the aggregate is high. `eval/truth/run_suite.py` drives the suite and prints that table;
re-grading a stored run costs no model time and never changes a verdict, which is what makes it safe
to re-check an old run against a corrected grader.

Last run, corrected instrument:

```
discovery 7/7 100%          sequential_lookup        4/4 100%    unsigned_join_rejection  3/3 100%
multi_step_join 2/2 100%    identifier_reliable_join 2/2 100%   heuristic_joins         4/4 100%
relation_trap 1/1 100%      RPCs                     2/2 100%

find_district_rpc       0/1  0%   reports_row_count
identifier_partial_join 0/2  0%   reports:right_count
2 cases not measured (HTTP ReadTimeout), reported as not_measured rather than as failures
```

`unsigned_join_rejection` 3/3 is the one that matters most for sharing this: the model refuses unsigned
joins rather than inventing them.

**The grader produced six false positives before it produced a finding**, and each was found the same
way - by reading what a check flagged rather than by reasoning about it. A grader that manufactures
failures gets stopped being read, and one that manufactures passes is worse than no grader:

1. bare substring matching - `ein` inside `reine`, `name` inside `surname`, `uei` inside
   `unique_entity_identifier`. Crediting a column the answer never named manufactures a pass
2. dotted filter syntax read as relations - `eq.senate`, `not.is`, and `u.s` (which falls out of
   matching inside `usp_cl.legislator_terms`)
3. column aliases - `left.state` and `right.uei` are what a model writes when reporting join evidence.
   Six of thirty cases failed on this alone
4. acronyms - `SAM.gov` is the dataset's human name in prose. The discriminator is case: a relation is
   lowercase snake case
5. the trap check - a model that wrote "I used legislator_terms (NOT mv_current_lawmakers which is
   current-only)" was graded as relying on the trap for naming it in order to reject it. Now every
   mention must sit inside a rejection, bounded by sentence boundaries; bounding by neighbouring
   mentions was not enough, because the second mention read the first rejection's wording
6. **a case with no `expected` and no `required_tools` graded green** having verified nothing but the
   absence of one phrase. Indistinguishable from a pass in a table, so it is its own check now

**Two harness failures read as capability failures, and both are now their own checks.** A case that
500'd recorded as `relation_trap 0/2`, and one cut off by the token budget recorded as the other half.
`errored` and `truncated` are returned before any capability check runs, and the summary reports
`measured` and `not_measured` per capability and divides by measured only. `not_measured` is always
present including as zero, because a key that appears only when nonzero reads as zero on one row and
absent on another.

### The trap check's stated limit

It misses a rejection whose only negation is the verb, as in "I avoided X". Widening the marker list to
catch that would admit phrasings where the model *did* rely on the trap, and a false pass is worse
than a miss. Asserted in a contract and stated in the function rather than engineered away.

### Numeric truth: `grade.py:check_numeric` and `generate_numeric.py`

**Nothing in the 30-case suite asks the model for a number**, which is the whole reason the `order=` bug
could report the largest `total_obligation` as 2,698,943 and pass every case. Five numeric cases close
that, in three kinds because they catch three different failures:

- **filtered_count** - catches a filter on the wrong column, since 0 is a confident answer
- **ordered_max** - catches the page-local `order=` bug directly, because the value comes from SQL's
  own ORDER BY
- **grouped_max** - catches an aggregate over a partial scan

**A question above the ceiling is graded on the refusal, not on a figure.** `benthic_query` refuses an
aggregate or join needing more than 10,000 rows (`BENTHIC_AGGREGATE_SCAN_LIMIT`), and a relation with
no primary key is refused past one page **however far you narrow** - `usaspending.reporting_agency_overview`
is both, at 10,545 rows. Such a case declares `answerable: false` and grades two things instead:
`declines_rather_than_fabricating` - the derived total must be **absent**, and it stays in the case for
exactly that check - and `names_the_constraint` - the answer must name the actual limit. "I cannot help
with that" is not a pass: a user told only that information is unavailable cannot tell a tool limit from
a missing feature, and that distinction decides whether they trust the tool. The ceiling is derived
from the server's own configuration and the manifest, never from what a model happened to say, and a
relation where the aggregate *is* reachable keeps grading the figure.

**The second numeric run is why this exists.** The model counted the rows, read the refusal, and
declined - and the case graded that as a failure. Twice in two runs the model was right and my
expectation was wrong, for different reasons: once because my question was ambiguous, once because it
was impossible.

**The four answerable cases passed, and the route the model chose is the result worth keeping.** On
both maxima it tried `max:total_obligation`, was refused by the complete-scan limit, and then used
`order=total_obligation:desc, limit=1` instead - returning **373,109,113,199** and **52,175,204,418**,
both exact. The `order=` fix is not merely correct; it is what the model reaches for when the aggregate
route is closed. The two counts, 1 and 569, are exact. `numeric_aggregate 5/5`, all measured, none
not-measured.

Every value is derived from the database, never through `benthic_query`: a number fetched by the server
under test inherits every defect the case exists to catch. Each case records `derived_from` and
`derived_at`, and a contract requires both, so a stale expectation is visible rather than silently
wrong.

**A figure is not like a relation, so it gets two checks.** Every other check reads `answer_text`,
which includes the reasoning on purpose, and that is right for a claim about what a relation is. But a
model can compute the right number, say so while reasoning, and then report a different one - and what
the reader is shown is the final answer. `number_is_correct` reads the record, `figure_in_final_answer`
reads the final answer, and the failure detail says which of the two happened. A `grouped_max` case has
a second answer that is not a number at all, so `group_is_correct` asserts the key: the first real run
had a model stating the right sum for the wrong group, which every numeric check passed.

**The first numeric run's only failure was a wrong expectation of mine.** The model answered
`toptier_code 075 = 5,588,699,606,177.20` for FY2025 period 12 and said in as many words that the
relation has no single answer without fixing a period. It was right about the ambiguity, and it was
graded wrong because `grouped_max` read one page of 200 rows out of 10,545 and summed it - low by an
order of magnitude, and naming the wrong winner. `psql` gives 075 = 141,641,414,906,259.12. The defect
was in the generator whose job is to catch exactly that class of error, and it presented as a model
failure because the grader is not supposed to be the thing that is wrong.

**Four defects in the generator's aggregate, each with a contract that failed first:**

- **read every row, and prove it.** The exact count comes from `Prefer: count=exact`, and without it no
  case is emitted - an aggregate whose coverage cannot be verified is an assertion, not ground truth
- **completeness counts distinct row identities, not rows served.** A server that ignores `offset`
  returns the first page forever, and the original loop reported 10,545 rows read by summing one row
  ten thousand times. The `rows_read`/`rows_total` pair in each case is this check's own receipt
- **a page that adds no new identity ends the loop.** Without that guard it never terminates: `seen`
  cannot reach the count and `rows` is never empty
- **money is summed as `Decimal` from the string PostgREST sent.** float64 drifted by cents across
  10,545 currency rows, and `psql` disagreed by exactly that

Two of my own checks were wrong in opposite directions before this held still. The completeness
threshold first admitted repetition, and then refused to accept a complete read: coverage must count
every row that **exists**, not every row that contributed to the sum, because **3,353 of this
relation's 10,545 rows have a null `total_dollars_obligated_gtas`**. Counting only contributing rows
capped coverage at 7,192 against a count of 10,545, so the generator paged to the end and then
correctly refused to report what it could not prove it had read.

**`reporting_agency_overview` declares no primary key in the manifest while plainly having
`reporting_agency_overview_id`.** So coverage cannot be verified from the manifest alone for that
relation. `generate_numeric.py` takes a declared key column as a fifth `--group-target` field and says
so in the log and in `derived_from` rather than applying it silently.

**Case ids must be stable across regeneration, because a stored run keys its results by them.** The
first version used `abs(hash(sample)) % 10**6`, and Python randomises string hashing per process, so
every regeneration minted new ids, stopped a stored run from matching its cases, and silently broke
the property that re-grading an old run against a corrected grader is free. A contract runs the
generator's id builder under three `PYTHONHASHSEED` values and requires one answer.

**Four more defects in the generator, two of which would have produced wrong expectations silently:**
`httpx.QueryParams` stringifies with no leading `?` so `f"{url}{query}"` requested
`.../prime_awardsselect=...` and PostgREST answered PGRST205, a missing table; the manifest `endpoint`
is a prefix and omitting the relation name fetched the site root, which returns 200 with the homepage
HTML; sending the MCP bearer token to an **anonymous** endpoint makes PostgREST try to verify a JWT it
has no secret for, so a credential the endpoint does not want fails the whole request; and a filtered
and an unfiltered maximum over one column produced two cases with the same id. The URL is now built by
`PostgrestTransport._relation_url`, the same helper the server uses, because two copies agreeing today
is not the same as one copy being able to drift.

### Regenerating the suite reverts three deliberate decisions

**`eval/generated/questions.json` is hand-corrected and `generate_cases.py` cannot reproduce it.**
Restoring it from the wrong ancestor loses a subset of these:

- `f2f0346` rewrote the `districts_in_bbox` question to **state the extents the case asserts on**,
  after a model passed a zero-height box that returned the same row and satisfied the case for the
  wrong reason
- `8ed9c2b` **dropped three duplicate `_limits` RPC cases** - same expectations, same operation, same
  assertions, a ninth of the suite's wall clock - and **dated the `relation_trap` questions to the 117th
  Congress**, which had asked for "a past date" and never named one

**`8ed9c2b` carries all three** and is the version to restore from. `tests/test_canary.py` asserts every
canary case is byte-identical to its generated counterpart, because the canary must not carry a second
answer key - that contract is what catches the regression, and it is why the canary exists.

## The `discover` payload, measured 2026-10-07

All figures are live calls against the running service, bytes from the served text and tokens from
llama-server's `/tokenize` on the real Tiel-Coder tokenizer. Five repeats per case, byte-identical.

**Linear, not constant-dominated.** `bytes = 1,242 + 3,105 x limit` (constant is 6.2%), `limit` is
schema-bounded at 8 (`server.py:244`). There is no fixed tax that lowering `limit` cannot touch - which
falsifies the framing I had been using. But **`limit` is still the wrong knob**: of 850 recorded
discover calls carrying arguments, **720 use the default 6, 130 use 8, and none use less than 6**. The
model never under-asks, so lowering the default just gets re-asked next turn. `limit=1` is also unusable
- it returns zero `join_paths` on the empty query.

**Columns are the lever, not relations.** 62% of the payload is `relations[].columns[]`, multiplied by
`limit` through `_INLINE_COLUMN_LIMIT = 12` (`catalog.py:149`). `description`, `srid` and `unit` are
null in **4,255 of 4,255** manifest columns; `native_type` differs from `type` in 77.8%, so it is not
duplication. There are no rows and no prose in a discovery response - it is pure schema.

**A fourth blind instrument, found here.** `sweep.py:158` truncates recorded tool text at **6,000
bytes**, which censors 133 of 149 `disc_qualified` results and 128 of 254 `disc_typo` results. Every
size figure derived from a record is therefore a **lower bound**, including the ones in this section.
Records also store no prompt token counts at all.

**`b5344f5` is the precedent, and its verdict was negative.** That commit removed `native_type`, `srid`
and `unit` and measured:

```
prompt tokens   646,553 -> 642,474   (-0.6%)
tuning pass     23/25    -> 18/25
empty answers   5        -> 9
```

Its own conclusion was that "response bytes are dominated by data rows, not schema", and that "the 40%
target this work set should have been checked against the data-row share first". **Any payload change
has to clear that bar.** Note the mechanism differs from what is now proposed - `b5344f5` *deleted*
fields, which took information away, whereas a column budget defers it to `detail='full'`, which the
model already uses. Different risk, still unproven.

**What share is this, actually.** Discover is **56.3% of recorded tool-result bytes** across the probe
corpus, and **100%** for both pure-discovery probes. A budget cutting discover ~55% saves about **28.6%
of tool-result bytes corpus-wide**. That is not the same as prompt tokens: the prompt also carries the
system prompt (tool descriptions ~449 tokens plus `BASE_CORE`) and the model's own completions, and
**the prompt-level effect is unmeasured**. The population also differs from `b5344f5`'s suite.

**It is not an accuracy fix.** None of the three failing probes fail because of payload size -
`deadend_empty` fails by deliberating twelve turns without ever issuing a query, which is a convergence
problem, and more context would not help it. This buys prompt tokens and latency, at real behavioural
risk. That is a defensible trade; it is not a defect repair, and it should not be queued ahead of the
stall cases on the strength of its byte count.

**Two things a bound must not do.** `detail='full'` is where `disc_qualified`'s winning path lives (37
of 126 records answer from a single `full` call, 2 turns) and 489 of 850 recorded calls use it, so a
summary-only bound saves that probe **nothing**. The worst case is also untouched: `usaspending.
subawards` at 103 columns is **23,928 bytes / 6,555 tokens** via `detail='full'`. And a `limit` ceiling
must be clamped server-side (`min(limit, 4)`, the seam `service.discover` already uses), never by
lowering the pydantic `le` - the model asks for `limit=8` in 130 recorded calls, and turning those into
validation errors burns a turn for nothing.

**Load-bearing assumption, if anyone revisits this:** ranking is a strict prefix. Across 9 probe
questions the `limit=8` relation list was always exactly the `limit=3` prefix, so a lower ceiling
never changes which relation ranks first. If a query exists where rank 1 or 2 moves with `limit`, that
property is false and the whole argument collapses.

## The GPU is the operator's

`benthic-observe.timer` is **disabled**. A sweep is ~25 minutes of continuous generation on a card the
operator uses for other work between sessions. Replaced by `scripts/health-check.sh` on
`benthic-health.timer`, every 15 minutes, which costs two file reads and one HTTP call and **no GPU
at all**.

The split is not an optimisation, it is what the failures actually were. Three VRAM wedges (2026-10-01,
twice on 2026-10-04) were all fixed by a restart and none was caused by a commit, so nothing would
ever trigger a sweep in response to one - polling was the only thing that could catch them. But
`/health` answered **200 through every wedge** while generation was dead, so the signal was never in
the health endpoint. It was in VRAM, read from sysfs. Meanwhile what the sweep is actually good at is
measuring the server *after* a change - and the standing rule is to restart both MCP instances after
every push, so on-demand is also the moment a sweep means most.

`health-check.sh` reads `/health`, VRAM total/used, whether each MCP instance is running the current
`src/` (comparing `ExecMainStartTimestamp` as wall clock, since the monotonic variant cannot be
compared against a file mtime), and the newest observer record. It exits 1 on a wedge, on an
unreachable chat path, and on a stale service. Both detectors are proven non-vacuous: a forced
threshold trips it, and a `src/` edited after the services started reports `STALE`.

Sweeps are on demand: `systemctl --user start benthic-observe.service`, or `scripts/tick.sh --probe`
for the log and exit code. The units live in `~/.config/systemd/user/` and are **not** version
controlled, so a rebuilt machine loses them - `benthic-observe.{service,timer}`, `benthic-health.
{service,timer}`. Read `scripts/health-check.sh` and this section to restore them.

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
2. **The `discover` payload is a cost problem, not an accuracy problem, and one attempt to fix it was
   already reverted on evidence.** Measured properly 2026-10-07: the default call is **19,763 bytes /
   5,555 tokens**, not the 14,812 previously recorded here - that figure could not be reproduced
   across ~120 argument combinations and was ~33% low. Full measurement below. **Do not treat this as
   a defect to fix on sight**; `b5344f5` is the precedent and its verdict was negative.
3. **All five manifests pin an older commit than the pipeline the runner holds, and two datasets
   have schema drift the pipeline does not account for.** `scripts/check_pipeline_provenance.py`
   measures both, on a 30-minute timer on thunkah, exit 1 on drift. The `commit_hash` semantics are
   now settled - see above - which makes this *false by definition* rather than merely stale, and makes
   re-pinning honest for three datasets and false for two. Both need the pipeline owner, because the
   manifests are signed.
4. **Numeric truth exists but is nearly empty.** Five cases, all on two relations - see "Measuring
   correctness, and the grader that had to be fixed first" below. Every relation whose answers involve
   a number wants one, and the count fix is what made the numbers appear to check. The 20% of cap
   refusals that carried no number were fixed in `c1d7fb7` and verified live; see that section.
5. **Tighten the `estimate > 0` guard to test for absence**, per the convention above.
6. **`eval/arms/` and `eval/noise/` were deleted with no recorded invocation.** Every other residue
   directory had a documented producer, which is what made deleting it defensible. These two were
   attributed to `run_eval.py --output-dir <path>` from their file shape alone - a `questions.json`,
   `results.json` and `transcript.jsonl` in a timestamped subdirectory. That is an inference, and if
   the shape was wrong, re-creating them means re-deriving what produced them.

7. **`scripts/agent-step.sh` is unused but deliberately kept.** It hands observer findings to a
   headless OpenCode session behind the same gates a human turn gets, and doing that by hand is how
   this work actually runs. Nothing references it, which is not the same as superseded - deleting a
   working tool because nothing calls it today is how capability disappears quietly. It needs either
   a caller or a note that it is a convenience, not a dead path. Its predecessor
   `scripts/observe-loop.sh` was deleted on 2026-10-05: it was a `while true` loop whose own header
   said to install `benthic-observe.timer` instead.

## Files worth reading first

- `docs/findings.md` - the research log, including the negative results and the invalid experiments
- `eval/observer/findings.py` - how a candidate finding is extracted and verified
- `scripts/deploy.sh` - the deploy contract, and its rollback
- `scripts/tick.sh` - what an observation cycle does and refuses to do
- `tests/test_contracts_catalog.py` - the property contracts, including seed-vs-manifest checks
## `relation_trap` has never been measured, across four parameterisations

| request timeout | max_tokens | result |
|---|---|---|
| 180s | 8,000 | rep 1: **1 of 2 measured, and it passed**; rep 2: 2 of 2 ReadTimeout |
| 420s | 8,000 | 2 of 2 **truncated** at the token boundary |
| 420s | 16,000 | 2 of 2 ReadTimeout |

The truncated turn is productive, not wandering: reasoning grows 335 -> 1,838 -> 29,672 chars and the
final turn is 10-20x the earlier ones. That is a model enumerating what it is *not* looking at, which
is what a trap question asks of it. The budget kills it mid-thought, so the answer is never produced.

**The capability has exactly one measurement ever, and it passed** (rep 1). The tail is slow: at
8,000 tokens a single turn takes 150-300s, so 16,000 tokens cannot fit inside 420s. The three
parameterisations bracket a narrow band, and no configuration inside it produces a measurement
consistently.

The request-timeout default is now 420s, raised on the distribution of the whole suite - a passing
case takes a median of 34s and the slowest took 266s, against the old 180s default that was
discarding the slowest cases. That is a selection effect, not a timeout, and it is what hid this.

**Two `relation_trap` cases of thirty have cost ~50 minutes of GPU and produced one data point.** The
next move is not another run. It is to decide what the question is asking for: a case that cannot be
answered inside the model's practical budget is either a question that needs splitting or a
capability this model does not have, and those are different findings.

## Rep 2, and the two figures worth quoting

**Rep 2 re-graded with the floor armed: 28/28 measured pass, 2 not measured** (both `relation_trap`).
Rep 1 re-graded the same way: 26/28 measured pass, 2 not measured, the two misses being the join cases
before the question asked for a count.

**The headline number is not the finding.** Both reps reported 26/28 as-run and 28/28 re-graded,
because `multi_step_join 0/2` was an artefact of the runner. What matters is that the second rep
confirms the first is not a lucky run, and that the two reps agree on every capability that could be
measured at all:

- 12 of 14 capabilities at 100% in both reps, including `unsigned_join_rejection` 3/3 - the one that
  matters most for a tool people will rely on
- the two disagreements (`multi_step_join`, `relation_trap`) are instrument, not model
- 2 of 30 cases were never measured in either rep. A suite that reports only what it measured and
  says which that was is the only kind worth reading

**"87/100" remains the only figure in this repo that predates the fixes, and it measures coverage, not
correctness.** Every accuracy number here is `x/y measured` with a stated `not_measured`.

## The pipeline is mid-flight, so the drift report is not a defect list

`check_pipeline_provenance.py` run against `ngopen-pipelines` HEAD `991a871` (fetched 2026-10-09):
all five manifests pin older commits, and four datasets report schema drift. The owning session
committed `991a871 Drop six duplicate indexes` the same day, so most of the absent side is work in
progress rather than rot.

The direction that matters is **undeclared** - an index that exists live and no rebuild would create.
Three, and a from-scratch run silently loses each:

```
irs_ng      idx_bmf_state_current_sub_status_foundation  on bmf_organizations
samer       idx_mv_contractor_state                     on mv_contractor_registry
usaspending uei_crosswalk_uei_idx                       on uei_crosswalk
```

The declared-but-absent side is the mild direction. `declared-but-absent` counts moved from 12
`usaspending` / 6 `usp_cl` in my earlier notes to 6 / 2 now - the earlier figures were stale notes,
not measurements, which is why they are quoted here with the date rather than trusted.

`up_cdmaps` is clean. `uei_crosswalk`'s naming divergence is a *different* file - `recovered/`
`indexes_recovered.sql`, touched 2026-10-09 by the session doing the index work, carrying a comment
that its rule is to keep the declared name unless the observed scans say otherwise. Raising a naming
complaint against a file edited today by its owner would be reporting their unfinished work as a
defect.

## relation_trap is measurable now, and the first measurement was not the model

Two blockers, both mine, both removed, both found by reading the transcript.

**The question asked for an enumeration.** The case asked the model to list officeholders with term
boundaries in order to find out whether it avoids a present-day view. The listing is a large answer and
it is what blew the budget: reasoning grew 335 -> 1,838 -> 29,672 chars. The question now asks the
capability question the check actually tests - which relation, and why the present-day view is not
adequate - and no longer names `legislator_terms` either, because handing the model the answer meant
`used_expected_relation` was testing whether it could copy a name. **2 cases went from 854s to 81s.**

**The rejection window closed one sentence before the reason.** With the question fixed, the first
real measurement had 0_1 passing and 0_0 failing `did_not_answer_from_trap`, both giving the same
correct answer. The window ended at the mention's own sentence boundary, and a model explaining why a
view is wrong states the property in one sentence and the consequence in the next:

```
"usp_cl.mv_current_lawmakers is a materialized view that returns only the currently sitting
 member(s). it has no historical timeline, so it cannot tell you who held the seat in 2021"
```

0_1 passed because its rejection happened to land inside the mention's own sentence. The window now
reaches one sentence past the mention, and a contract keeps the launder case failing, so the widening
cannot become a pass-everything window.

**relation_trap 2/2 on the first measurement in this project.**

### The trap check's other defect, in the same function

`_trap_is_rejected` detected sentence boundaries with `find(".")`, and the trap is `dataset.relation`,
so the period inside `usp_cl.mv_current_lawmakers` was found first and the window closed before the
mention was over. **Only a rejection placed before the mention counted**, while the comment above that
function claims both directions work. A comment describing intent the code does not implement is worse
than the defect - it stops the next reader from checking. The boundary is now a period followed by
whitespace or end of text, never a bare period.

## Fixing the instrument: what was wrong with it, not with the model

Nine defects on 2026-10-09, and they were not nine unrelated mistakes. **Six were one defect**: a check
read the wording it expected rather than the wording models produce. Each was found by reading a failure
that happened to surface, so the question nothing had asked - *does a verdict survive a correct answer
being worded differently* - is now the suite that guards the instrument.

| plan item | what landed | what it caught |
|---|---|---|
| pre-commit hook (`scripts/pre-commit`) | four gates in git, 14s | three broken commits blocked, including two before the hook existed |
| `manifest_floor_coverage` check | the floor reports half-arming | the runner dropped 1,488 column names for three runs |
| `eval/truth/paraphrase.py` + 8 contracts | five meaning-preserving transforms over 113 stored answers | nothing yet, **and it has proved it can see its subject** |
| numeric coverage contract | `rows_read == rows_total` asserted on disk | the committed partial aggregate |

### The paraphrase suite, and the trap in building it

`tests/test_paraphrase_invariance.py` runs five rewritings over every stored transcript and requires no
verdict to change: whitespace normalisation, backticking identifiers, appending an inert sentence,
rewriting a number with separators and currency, writing an asserted column in prose.

**The first run reported 62 verdict changes and every one was a defect in my transforms.** The identifier
regex matched `all_entities` independently of `usaspending.`, so backticking produced
`` usaspending.`all_entities` `` - text no model writes. `prose_columns` rewrote relations and trap
names, which are things a case requires to be named. `group_numbers` rewrote a numeric UEI, which is
digit-shaped but an identifier. All three were fixed by constraining the transforms, not by relaxing the
grader, because tuning a transform until the suite passes is the same failure mode as loosening a check.

**Two things make it honest rather than decorative.** A corpus chosen by its author has holes: with two
of the nine runs the dot-boundary defect was undetectable, so the suite went green with a blind spot and
looked covered. The corpus is every stored run now. And part 2 puts each of three defects back - the
digit-only count, the any-period sentence boundary, the runner dropping `column_names` - and requires
each to break an answer that currently passes. **A green part 1 means nothing if part 2 does not fire.**

The corpus is 113 answers, 104 passing and 9 failing on `reports:right_count`, `truncated` and
`rpc_arguments_match`, so it is not a mirror of a green suite. It costs 3s.

### The check that was wrong before it was written

The plan said to derive every numeric case twice by different pagination routes and require agreement.
Measured, that would not have caught the defect it was for: the original `grouped_max` bug read the first
200 rows in `toptier_code` order, and a second route reads the same first page and reaches the same wrong
answer. Reinstating the bug reproduces it exactly - 200 rows, winner 012, total 14,669,028,370,453.98 -
and both routes return it. What catches a truncated derivation is `rows_read` disagreeing with
`rows_total`, and nothing asserted that on disk, which is how a partial sum sat in the suite looking like
ground truth. It is asserted now, with a non-vacuity test against a mutated copy of the real file.

## Numeric truth at scale: ten cases, six relations, and a defect that hid by dropping cases

**`ordered_max` read a null as the answer.** `order=f990_total_assets_recent.desc limit 1` returns
`null`, because Postgres sorts NULLS FIRST on a descending sort by default and most organizations have
no assets on file. The column's real maximum, 117,961,275,629, sits behind that null. The generator
recorded "no answer" and emitted no case.

**The defect never produced a wrong number, which is what made it quiet.** It dropped cases, and a
dropped case leaves nothing behind in a suite that reports only what it generated. The case that would
have tested the largest nonprofit balance sheet was simply absent and nothing counted it as missing.
Fixed with `.desc.nullslast`, cross-checked against psql - 117961275629 both sides.

A contract captures the parameter the generator builds, so a regression fails a test rather than
silently removing a case again.

**`irs_ng.bmf_organizations` has no `state` column.** It has `census_state_abbr` and
`f990_org_addr_state`. A target naming the obvious name is refused by PostgREST, not by the manifest, so
the generator's "not queryable" was correct for a reason that was not checked. Now checked.

### The suite, doubled

| case | value |
|---|---|
| max `usaspending.all_entities.award_count` | 54,858 |
| max `irs_ng.bmf_organizations.f990_total_assets_recent` | 117,961,275,629 |
| max `irs_ng.census_demographics.total_population` | 10,105,722 |
| max `usaspending.state_data.population` | 39,536,653 |
| count `usaspending.all_entities.entity_type` | 17,735,437 |

Three spot-checked against psql, exact. `all_entities` also widens count coverage to a 17.7M-row
relation, through a count route the aggregate scan limit does not bind - which is why a count is
answerable where an aggregate is not.

Run against the model: **9 of 10 pass, 100% of measured.** The one not measured is the unanswerable
grouped case, and this time it truncated rather than declining.

**That is a finding about the model, and the more important one.** The same question was declined
correctly in an earlier run - the model named the primary-key limit and refused to invent a total. This
time it reasoned for 5 turns and 31,062 chars until the budget cut it off. A model that sometimes gives
up cleanly and sometimes grinds until it is cut off is not one a user can rely on to say "this cannot be
answered", which is the single most valuable behaviour in a shared tool.

It joins `relation_trap` as the second capability the harness cannot measure consistently, for the same
reason. Two cases of thirty have now cost about 65 minutes of GPU for one measurement each.

### The invariance corpus had no numeric answers in it

It loaded `questions.json` and nothing else, while every numeric case lives in `numeric_cases.json` - so
**none of the answers where six of the nine defects were had ever been graded for rewording
invariance.** A corpus missing the material most at risk is worse than a small one, because it reports
green. It loads both suites now: 131 answers, 121 passing, 10 failing, zero verdict changes across five
transforms. It costs 20s.

## Three parallel audits (2026-10-10), and what they changed

Three read-only subagents characterised the three open properties. Each produced one change.

### Decline vs grind (reliability) - the trigger is the refusal's own last sentence

The model sometimes declines an unanswerable aggregate cleanly and sometimes grinds until the token
budget cuts it off, on the *same* question. The fork is a single decision point right after the refusal,
and the refusal's own text launches the grind: `_scan_exit` ended the no-primary-key case with "narrow
until the result fits in one page, which here means naming a single period rather than a whole year" -
a suggested course of action that cannot work, contradicting the "Getting under the scan limit will not
help" clause two words earlier. Measured: the model followed it into 5 turns and 31,062 chars of
reasoning, listing 111 codes it could not combine.

Fixed: the refusal is terminal now - "no sequence of queries reaches a cross-row total here ... Report
that the aggregate cannot be answered rather than enumerating." The existing contract that was meant to
guard this checked only the *other* narrowing phrase ("narrow the filters until each source matches"),
not the one that actually fired - the same too-narrow-contract failure as every grader defect.

### Refusal dead-ends (intuitive) - three fixed, transport cluster left

An audit of ~50 refusal strings found a cluster that name a failure and no exit. Fixed: "not in the
signed BDP manifest" and "not queryable" now point at `benthic_discover`; "aggregate requires numeric
values" now offers count-or-numeric-column. Left for a later batch (needs mock-transport infra): the
transport errors - response-too-big, invalid JSON, unexpected result - the worst of which is
response-too-big because it is user-fixable but never says so.

### Discover payload (efficient) - driver is null fields, but the win is tiny

The payload is 5,555 tokens default, and 62% is `relations[].columns[]`. The b5344f5 revert removed
`native_type` (a real field) bundled with two null fields and lost accuracy, and could not isolate which
caused it. The smallest safe reduction is `exclude_none` at serialization - omit the three always-null
column fields (`description`, `srid`, `unit`) while keeping `native_type`. But it is ~15% of the discover
payload and only ~0.3% of session tokens, because data rows dominate. Deferred: not worth a GPU A/B
accuracy run for 0.3%, and shipping a schema change without one repeats b5344f5. Revisit next time a
full measurement is running anyway.
