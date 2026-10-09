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

**The one probe still worth attacking is `join_partial`, at 4/40.** It is the lowest and it is not
explained by anything fixed today. Note it has only 40 runs against 80+ for the others, so its rate
rests on half the evidence and should be attributed before it is trusted.

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
4. **20% of cap refusals carry no number, 96% of those on one relation.** `usaspending.prime_awards`,
   183M rows. Highest-leverage accuracy fix available. Mechanism not yet diagnosed, and the `reltuples`
   shortcut is explicitly ruled out - see "Two accuracy defects" above.
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