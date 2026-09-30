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

## Next steps, in order

1. **Rewrite or retire the three unanswerable probes** so the failure count means something.
2. **Check whether `rpc_*` regressed**: `rpc_box` 6/8 and `rpc_point` 7/8, from a previous 8/8.
   Either new guidance hurt or it is variance; cheap to test and a real regression matters.
3. The scan wall is x47. The refusal stays; only guidance naming aggregate-free routes remains.
4. Investigate the recurring hallucinations: `legislator_term` x9, `count` x8 (an aggregate alias
   put into `select` - the server is right), `registrations` x7, `terms` x5.
5. Do not chase the eleven probes that vary run to run. That is the signal already known not to
   trust.

## Files worth reading first

- `docs/findings.md` - the research log, including the negative results and the invalid experiments
- `eval/observer/findings.py` - how a candidate finding is extracted and verified
- `scripts/deploy.sh` - the deploy contract, and its rollback
- `scripts/tick.sh` - what an observation cycle does and refuses to do
- `tests/test_contracts_catalog.py` - the property contracts, including seed-vs-manifest checks