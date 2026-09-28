# benthic-mcp

A read-only MCP server for signed [Benthic Data Provenance](https://benthic.io/bdp/) datasets. It is designed for the llama.cpp Web UI and exposes a normal browser-side Streamable HTTP MCP provider on the LAN.

The server exposes four tools:

- `benthic_discover`: finds the smallest relevant signed relation, columns, and join paths.
- `benthic_query`: runs a bounded single-relation query with compact filter, aggregate, and `having` expressions.
- `benthic_join`: runs an advanced multi-relation plan using only signed BDP join paths.
- `benthic_rpc`: runs three allowlisted spatial RPCs.

Plain-language questions are interpreted by the chat model. The model converts a question into the compact tool arguments; this service validates and executes them without an embedded LLM.

## Requirements

- Python 3.12 or newer
- `uv`
- Network access to `https://benthic.io`
- A local `llama-server` build with Web UI support

Install the locked environment:

```sh
/home/otherdrums/.local/bin/uv sync --frozen
```

## MCP service

The HTTP service listens on port 8082 by default. Copy [`config/benthic-mcp.env.example`](config/benthic-mcp.env.example) to `~/.config/benthic-mcp/env`, replace the token placeholder with a random value of at least 32 characters, and keep the file mode at 600.

The persistent user service can then be installed with:

```sh
install -m 644 scripts/benthic-mcp.service ~/.config/systemd/user/benthic-mcp.service
systemctl --user daemon-reload
systemctl --user enable --now benthic-mcp.service
```

The endpoint is:

```text
http://192.168.10.222:8082/mcp
```

The health endpoint is `http://192.168.10.222:8082/health` and requires the same bearer token.

## llama.cpp Web UI

Start llama-server with [`scripts/start-llama-mcp.sh`](scripts/start-llama-mcp.sh). It preserves the ROCm environment and model settings but does not start a server-side stdio MCP process. Benthic is configured as a normal Web UI MCP provider instead.

In the Web UI, add a Streamable HTTP provider with:

- URL: `http://192.168.10.222:8082/mcp`
- Header: `Authorization: Bearer <token from ~/.config/benthic-mcp/env>`
- CORS proxy: disabled

The checked-in [`config/llama-mcp.example.json`](config/llama-mcp.example.json) contains the provider shape without a secret. The Web UI stores the provider configuration locally; do not commit the real token.

Other remote MCP providers, such as Exa, Context7, and GitHub, can be added independently. The optional llama.cpp CORS proxy is not needed for this LAN provider.

## Agent workflow

A typical request is handled as follows:

1. The model calls `benthic_discover` with the user's domain terms.
2. It calls `benthic_query` with the returned `dataset.relation` and exact columns.
3. The service fetches only signed, anonymous PostgREST relations.
4. Aggregates are computed from complete source scans or rejected; truncated source rows are never silently totaled.
5. The model answers with the returned provenance, completeness flag, and warnings.

Single-relation expressions use these forms:

- `where`: `action_date=gte.2022-01-01`, `recipient_state=eq.CA`
- `metrics`: `total=sum:award_amount`, `rows=count:*`
- `having`: `total>100000`
- `order`: `total:desc`

Filters are conjunctive. Multiple filters on one column are sent as a bounded PostgREST `and` expression. The service automatically uses signed primary-key ordering for complete scans.

For historical officeholder questions, query `usp_cl.legislator_terms` with term bounds. `mv_current_lawmakers` is explicitly current-only and is not a substitute for historical terms.

## Trust and safety

The adapter:

- pins the current Ed25519 authority key;
- verifies RFC 8785 canonical JSON, SHA-256 payload hashes, and Ed25519 signatures;
- verifies every member manifest against the collection hash;
- uses only relations marked `queryable: true` in signed manifests;
- rejects arbitrary URLs, relations, SQL, and write operations;
- restricts API and RPC traffic to HTTPS endpoints on `benthic.io`;
- requires a bearer token and exact Host/Origin checks for HTTP transport;
- enforces source, result, timeout, response-byte, and complete-scan limits;
- caches only previously verified BDP documents;
- labels partial and heuristic joins in every result.

The fetched `keys.json` is not used as a trust root. Add rotated trusted keys through `BENTHIC_TRUSTED_KEYS` only after out-of-band verification.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BENTHIC_BDP_ROOT` | `https://benthic.io/bdp` | Signed BDP document root |
| `BENTHIC_COLLECTIONS` | `ngopen` | Signed collections to load |
| `BENTHIC_TRUSTED_KEYS` | Current pinned key | Trusted Ed25519 public keys |
| `BENTHIC_CACHE_DIR` | `$XDG_CACHE_HOME/benthic-mcp` | Verified catalog cache |
| `BENTHIC_CACHE_TTL_SECONDS` | `900` | Refresh interval |
| `BENTHIC_MAX_CACHE_AGE_SECONDS` | `604800` | Maximum stale-cache fallback age |
| `BENTHIC_REQUEST_TIMEOUT_SECONDS` | `30` | Upstream request timeout |
| `BENTHIC_MAX_ROWS` | `1000` | Maximum PostgREST page size |
| `BENTHIC_DEFAULT_QUERY_LIMIT` | `100` | Default result limit |
| `BENTHIC_AGGREGATE_SCAN_LIMIT` | `10000` | Maximum complete source scan |
| `BENTHIC_MAX_RESPONSE_BYTES` | `1048576` | Per-response byte limit |
| `BENTHIC_MCP_HOST` | `0.0.0.0` | HTTP bind address |
| `BENTHIC_MCP_PORT` | `8082` | HTTP port |
| `BENTHIC_MCP_PATH` | `/mcp` | Streamable HTTP path |
| `BENTHIC_MCP_ALLOWED_HOSTS` | LAN and loopback hosts | Accepted Host headers |
| `BENTHIC_MCP_ALLOWED_ORIGINS` | `http://192.168.10.222:8081` | Exact browser origins |
| `BENTHIC_MCP_BEARER_TOKEN` | unset | Required HTTP bearer token |
| `BENTHIC_PLAYBOOK_MODE` | `seed` | `off`, `seed`, or `active` |
| `BENTHIC_PLAYBOOK_PATH` | `<cache>/playbook.json` | Promoted playbook location |
| `BENTHIC_PLAYBOOK_TOKEN_BUDGET` | `600` | Token cap for the always-on core slice |
| `BENTHIC_TRACE_ENABLED` | `1` | Record objective tool-call traces |
| `BENTHIC_TRACE_RETENTION_DAYS` | `30` | Trace retention |
| `BENTHIC_LESSON_RETENTION_DAYS` | `30` | Lesson retention |
| `BENTHIC_TRACE_INCLUDE_TEXT` | `0` | Store question text from `benthic_report` |
| `BENTHIC_CONSOLIDATOR_LLM_URL` | local llama-server | Model used by the consolidator |

## Development

Run local quality checks:

```sh
/home/otherdrums/.local/bin/uv run ruff check .
/home/otherdrums/.local/bin/uv run ruff format --check .
/home/otherdrums/.local/bin/uv run pyright
/home/otherdrums/.local/bin/uv run pytest -m "not live"
```

Run opt-in live tests against Benthic:

```sh
BENTHIC_LIVE_TESTS=1 /home/otherdrums/.local/bin/uv run pytest -m live
```

The large daily award regression is opt-in separately:

```sh
BENTHIC_LIVE_TESTS=1 BENTHIC_LIVE_REGRESSION=1 /home/otherdrums/.local/bin/uv run pytest -m live
```

## Evaluation

The evaluation suite is generated from the verified signed catalog rather than a fixed NGOpen list:

```sh
/home/otherdrums/.local/bin/uv run python eval/generate_cases.py
/home/otherdrums/.local/bin/uv run python eval/run_eval.py
```

`eval/generated/questions.json` and `questions.md` are the generated cases. Future manifests
automatically add cases for new join paths, RPCs, and relations.

Case families:

| family | what it probes |
| --- | --- |
| identifier and heuristic joins | every signed path, with independent oracle probes |
| partial-context joins | a partial path without its required context predicates |
| spatial RPCs | each allowlisted operation, rows and completeness flags |
| discovery | which relation and fields the catalog points at, spread across datasets |
| sequential lookup | two relations that share a column but have no signed join |
| unsigned-join rejection | the model must decline rather than invent a relationship |
| multi-step join | two signed hops through a hub relation, both of which must be walked |
| relation trap | a present-day view asked a historical question, which answers confidently and wrongly |

The last two exist because the simpler families saturate. A current-only view returns a plausible
answer, and a two-hop chain punishes guessing one identifier path, so both fail in ways a
tool-call-shape check cannot see. Counts are tunable with `--discovery-cases`, `--sequential-cases`,
`--multi-step-cases`, and `--trap-cases`.

Each run stores the generated cases, model transcript, tool arguments/results, token usage, timings, failures, and report under `eval/runs/<run-id>/`. The runner uses the local llama-server at `192.168.10.222:8081` and the authenticated MCP provider at `192.168.10.222:8082/mcp`.

## Playbook and self-improvement

The playbook is the dataset-specific instruction set the calling LLM reads instead of
rediscovering the schema. It stores prose only: what a dataset is for, which columns to
prefer, which mistakes to avoid, and lessons learned at runtime. Facts the signed catalog
already carries, including relation lists, column types, join paths, and RPC endpoints, are
always read from the verified manifest at serve time, so guidance cannot contradict it.

Guidance reaches the model through two channels. The always-on core slice is written into
the `benthic_discover` description, because the llama.cpp Web UI forwards tool descriptions
but not the server `instructions` field. The full per-dataset detail is served on demand by
`benthic_playbook`.

The loop has five steps:

1. The server records an objective trace of every tool call: tool name, outcome, error text,
   row count, and truncation. Argument values are never stored, only argument names.
2. A calling LLM that got itself stuck calls `benthic_report` once with the symptom and the
   correction. The server attaches the recent trace summary and stores the lesson as
   `pending`. Repeat reports on the same symptom are merged and counted.
3. `eval/consolidate.py` merges, dedupes, and prunes the pending lessons, and optionally asks
   the calling model to compress them into short core rules and anti-patterns. Every sentence
   it returns is screened against the signed catalog; ungrounded references are dropped and
   counted.
4. `eval/promote.py` runs the real MCP tool surface twice against the same LLM, once with the
   current playbook and once with the candidate, and promotes only if the candidate raises the
   pass rate on the tuning cases, regresses no case, adds no forbidden claim, stays inside the
   tool-call and p95-latency budgets, and does not lose ground on the holdout cases.
5. Promotion writes the new playbook, marks the promoted lessons active, and asks for a
   service restart. `MCPServer.instructions` is static in mcp 2.2.0, so a restart is how a new
   playbook takes effect.

Two boundaries are deliberate. Lessons are stored individually, never as one blob, so a
regression can quarantine the offending lesson instead of rolling back the whole playbook. And
the RPC allowlist in `src/benthic_mcp/rpc.py` stays hand-written: it decides which upstream
function gets called, so a model-authored playbook must never be able to widen it.

```sh
/home/otherdrums/.local/bin/uv run python eval/consolidate.py     # pending -> candidate
/home/otherdrums/.local/bin/uv run python eval/guard.py           # holdout tripwire, rolls back if needed
systemctl --user restart benthic-mcp.service
```

### What has been measured, and what has not

| change | result |
| --- | --- |
| **Disabling the model's reasoning** (`chat_template_kwargs.enable_thinking=false`) | **Kept, and the largest single effect measured.** `relation_trap_0_1` 0/4 to 3/4, the case that had failed in every run of this project. Suite 28/33 to 30/33 with zero regressions, completion tokens -68%, prompt tokens -19% |
| Answer-delivery rule in the always-on core | **Kept.** 2/5 to 5/5 on the case that failed by turn exhaustion, 5/5 to 4/5 on a case that already passed, net 7/10 to 9/10 over the pair |
| Unknown-column candidates plus `detail='full'` | **Kept.** Adopted 25 times in eight runs; the error message demonstrably recovered a session that had guessed five wrong columns |
| Path lookup on `benthic_playbook`, `benthic_join` self-resolving | **Kept.** Discovery before first use 5.0 to 3.0 on one stuck case; the common join is one call instead of search-then-join |
| Capping `max_tokens` instead | **No effect.** 0/4, 0/3 and 0/3 at 4096, 2048 and 1024, and `finish_reason: length` got *more* common as the budget fell |
| A system prompt telling the model to answer immediately | **Ignored outright.** Reasoning came back at 1,225 characters against 1,163 for no system prompt at all |
| Per-response answer nudge on complete results | **Reverted.** Worse than the seed on every case measured: 2/5 and 2/5 and 0/5, against 5/5 and 2/5 for the seed |
| Loop-breaking rule in the always-on core | **Reverted.** 0/6 to 0/8 on the two cases it was written for, both still returning no answer |
| More turn budget instead of a rule | **No effect worth having.** 4/15 to 5/15 passing when max-turns went from 5 to 9, empty answers 10 to 7 |
| Dropping read-nothing schema metadata from responses | **Reverted.** A 31% cut to discovery responses moved the whole run's prompt tokens 0.6%, and tuning accuracy fell 23/25 to 18/25 |
| Twelve accumulated lessons over the answer rule | **No difference.** Both scored 9/10 on the same two cases |

### The two hard cases were the model, not the server

Five server-side interventions had failed to move `multi_step_0_1` and `relation_trap_0_1`, so the
only thing left to vary was the model. It paid immediately.

| | thinking on | thinking off |
| --- | --- | --- |
| `relation_trap_0_1` | 0/4 | **3/4** |
| `multi_step_0_1` discovery before first use | 3.0 | **1.5** |
| `multi_step_0_1` completion tokens | 5,953 | **1,190** |
| golden suite, regression check | 24/24 | **12/12** |

Capping `max_tokens` does not work, and its failure is the clue: `finish_reason: length` became
*more* common as the budget fell, because the model was not near the end of a long answer - it was
looping through calls and being truncated earlier. A system prompt is ignored outright. Only the
chat-template switch works, and it works completely, taking reasoning to zero characters.

Across the full suite that is 30/33 against a 28/33 baseline, two cases gained and none lost,
completion tokens down from 63,737 to 20,461, prompt tokens down 19%, empty answers 5 to 3, and
discovery before first use roughly halved.

It is a **client-side** option, so this is a deployment instruction for whatever calls the MCP, not
a server fix. `llama-server` cannot set it for its callers.

Every failure in the suite is a failure to deliver an answer, not a wrong answer, a bad column, an
invented join or a missed relation. That is what the kept rule targets, and it moved the worst case
from 2/5 to 5/5.

The nudge is the informative failure. The same instruction delivered once in the always-on core is
respected as a standing constraint; the same instruction delivered again on every completed result
competes with the task and produces premature answers, which is worse than saying nothing. Delivery
frequency mattered more than delivery reliability.

The metadata trim is the informative failure about performance. The reasoning was sound: every byte
a tool returns is re-sent on each remaining turn, so response size is multiplied by conversation
length, and prompt tokens run 10:1 over completion. The measurement was wrong. Response bytes are
dominated by data rows, not schema: a hundred-row query is about 7KB against a 3.4KB discovery
response, so trimming schema metadata bought 0.6% and cost five cases. The fields may well be
carrying weight the model uses when choosing a type or a spatial predicate. Cost per column is now a
test, so a large future addition is caught, but no field is removed on the argument that nothing
reads it.

### Joining two datasets, and why the fix was addressing rather than memory

The join graph is a handful of signed edges, so every answer to "how do I get from relation A to
relation B" is a pure function of the signed catalog. Nothing needed memising; the problem was that
the only way to ask was a natural-language search, and a search can be rephrased. A model that asked
for a path which does not exist got an answer indistinguishable from one that was worded badly, and
the only available move was to ask again.

`benthic_playbook(from_relation=, to_relation=)` returns the route hop by hop, shortest first, with
the exact `benthic_join` arguments and nothing else - 1,958 bytes naming the hub, against 8,580 bytes
listing every edge and leaving the caller to spot the route. A route that does not exist is a bounded
negative naming what the source relation *is* signed to, never silence. `benthic_join` resolves its
own path when the columns are omitted, so the common case is one call instead of search-then-join,
and it resolves only a single reliable identifier edge: a heuristic, partial or spatial join is never
chosen for the caller even when it is the only one.

Measured on the two cases that were spinning on discovery, discovery calls before the first query fell
from 5.0 to 3.0 and from 3.5 to 3.3, both still 0/4. The lookup was used on 3 of 11 playbook calls
while `benthic_discover` ran 28 against its 3. So it helps and is not sufficient, which is consistent
with the four guidance interventions that have also failed to move these cases.

Rediscovery is recorded per case in `rounds.json` - discovery calls before the first query, repeated
`(tool, source)` pairs, calls per case, and calls among sessions that answered - because pass rate
cannot see it. A session spinning on discovery scores exactly like a working one.

### Two regressions this work introduced, and their fixes

`eval/golden/` is the harness's check on itself, and it caught both of these.

Adding the route lookup made the golden unsigned-join case fail 3/6 while the model was answering
*correctly* - it used the new lookup instead of discovery. The case pinned `benthic_discover` as the
required tool, and there are now two correct routes to that answer, so the case was wrong rather than
the server. `run_eval` gained `required_tools_any` so a case can accept either route, and the golden
case uses it.

The discover description gained a sentence advising the model to prefer a guessed column over reading
the column list. On a case whose whole point is using discovery to get columns that made things
worse: 3/6, five discovery calls and no answer. The advice is now the opposite, and reads the columns
from discovery with `benthic_query`'s near-miss candidate as the recovery step rather than the plan.
With both fixed the golden set went 18/24 to 23/24, and the single remaining failure is one repetition
of an otherwise stable case.

### The column dead end, and the two cases that are not about it

Discovery lists at most 12 columns per relation, 21 of the 119 signed relations have more than 40
and one has 374, and an unknown-column error used to name only the rejected column. A model that
guessed wrong therefore had one move: guess again. `benthic_query` now returns ranked near-miss
candidates drawn only from the signed manifest, so a suggestion can never name a column that does not
exist, and `benthic_discover` takes `detail='full'` to list every column of one named relation. The
model adopted the new parameter 25 times in eight runs.

It did not fix `multi_step_0_1` or `relation_trap_0_1`, which still return no answer in 8 of 8
repetitions, and the traces say why. Both call `benthic_discover` five or six times and never call
`benthic_query` at all. They are not missing information: asked for the signed path from
`usaspending.all_entities` to `samer.sam_registrations`, discovery returns
`all_entities.uei = sam_registrations.uei`, reliable, with a note. The model is told the right thing
repeatedly and does not act on it, in runs that burn 8,773 completion tokens reasoning and end on
`finish_reason: length`.

So these two were not addressable from the server. Four server-side interventions failed to move
them, and the fix turned out to be the model's reasoning mode: with `enable_thinking=false` one of
them goes 0/4 to 3/4. What was needed was not better advice but a model that answers instead of
deliberating.

The core slice admits three playbook items and the cap is applied twice, in `verify()` by screened
sentence and again in `render_core()`. The second cap is the binding one, so a seed rule longer
than one sentence is dropped from the guidance with no error raised. `tests/test_playbook.py` now
asserts every seed rule survives into the served slice.

### Accumulating over usage

Voluntary `benthic_report` uptake measured at 2%, so a lesson is instead produced by a reflection
turn: the server detects an objective struggle from the trace log, and the harness asks the same
model that just failed what a future session should do differently. Everything happens under
`BENTHIC_CACHE_DIR`, so a run never touches the live store.

`benthic_playbook` uptake is a different matter and is healthy: 41% of runs call it, and on the
cases that fail it is closer to 100%. The model does seek guidance when it is stuck. It reads that
guidance early, at call 2.4 of 6.5 on the runs that run out of turns, so guidance is not arriving
late either. What the traces show instead is that the model reads the advice and then repeats the
same class of call anyway: failed runs average 2.0 repeated tool calls against 0.2 for runs that
pass. The dominant failure is non-convergence, not missing knowledge, and prose advice does not
change which action gets selected next.

```sh
/home/otherdrums/.local/bin/uv run python eval/harness.py --rounds 6
```

Each round runs the generated questions against the accumulated playbook, reflects on what went
wrong, records the lessons, consolidates them, and serves the result to the next round. A round is
one repetition of the suite; raise `--reps` only for measurement, since it multiplies cost.

Long runs belong in tmux so they can be watched:

```sh
scripts/tmux-run.sh benthic-improve logs/improve.log \
  /home/otherdrums/mcp-tools/.venv/bin/python eval/harness.py --rounds 6
tmux attach -t benthic-improve      # or: tail -f logs/improve.log
scripts/tmux-ls.sh                   # session summaries
scripts/tmux-stop.sh benthic-improve
```

The reflection prompt is built without the case oracle, and two guards enforce it: quoted JSON keys
that only the oracle document would contain, and any oracle value the agent could not have seen.
The second guard exists because a model may pick the same words for its own arguments, so field
names alone are not evidence of a leak.

Struggle detection uses only what the server can observe. `truncated` is deliberately not a signal,
because it also means "the agent asked for fewer rows than exist"; `source_complete is False` is
the real hazard and is what gets flagged. A failed strict score is added by the harness, which sees
the final answer, so a confidently wrong answer with clean tool calls still produces a lesson.

The tripwire compares the accumulated playbook against the last known good on the holdout split.
It is a regression check, not a measurement of improvement: eight cases at two reps cannot resolve a
small gain, and six of the eight sit at 2/2 in both arms, so the movable range is two cases. A
regression is called only when the aggregate drops and at least two cases lose ground, or one case
collapses outright. On the first run the whole verdict rested on two single-rep flips, which is the
noise level this check exists not to over-read.

### Requiring evidence before a lesson can be served

Promotion used to require only that a lesson was `pending` and its catalog fingerprint matched. That is
a correctness check: it says the statement names things that exist, not that it changes what a model
does. Nothing in the loop ever asked the second question.

`eval/attribute_pending.py` now measures a lesson against the case it came from, alternating repetitions
between the two arms, and records the verdict on the lesson itself. A lesson is served only when that
verdict is `fixes`, and the store keeps the case, the repetition count, and the lesson that donated the
evidence, so any served line can be traced to the measurement that put it there.

Running the gate against the twelve lessons that accumulation had actually produced:

| verdict | count | what it means |
| --- | --- | --- |
| fixes | 0 | |
| regresses | 1 | 3/3 -> 1/3, quarantined |
| no failure to fix | 3 | source case already passes, so the lesson was never given a chance |
| inconclusive | 2 | |
| unmeasurable | 6 | four had no source case, two came from the holdout |

So accumulation was not working: nothing in twelve lessons earned a place, and one made a case
measurably worse. The served document drops from twelve lessons and eight distilled core lines to zero
lessons and the three seed rules.

The verdict that took the most care to get right is the difference between *no effect* and *nothing
left to fix*. A lesson learned from a case that something else has since fixed cannot be judged there,
because there is no failure for it to remove, and calling that "no effect" throws away good advice. The
answer-delivery rule was discarded that way once. A saturated case is now reported as its own verdict,
and a stalled lesson is re-measured against a case that still fails. Regression is still checked first,
so a lesson that breaks a passing case is never mistaken for an untestable one.

Four holes in the gate were found by running it rather than reading it, and each is a way a
self-modifying loop can be talked into serving something it should not:

- the gate only looked at `pending` lessons, while the twelve were `active`
- consolidation carried the document forward, so it was self-perpetuating and no measurement could
  ever reach what was already in it
- promotion marked every lesson in the document active, silently undoing the gate's quarantine
- the arm under test was built by removing a lesson that is, correctly, absent from the document
  until it has been measured

`tests/test_loop_invariants.py` states these as properties rather than examples: nothing is served
without a measured effect, a verdict is auditable, inherited evidence names its donor, an unrelated
lesson inherits nothing, the document cannot self-perpetuate, the core is rebuilt from the seed when
nothing survives, and a lesson the catalog rejects is dropped even with evidence.

### The ceiling: per-case attribution cannot see a general rule

The answer-delivery rule is the clearest thing in the store, and the gate still could not promote it.
Re-pointed at the one tuning case that still fails, it measured 0/3 -> 0/3. Yet it is independently
worth two or three cases: removing it from the always-on core drops the suite from 30/33 to 27/33 with
thinking off, and from 28/33 to 26/33 with it on.

Both measurements are correct and they are not in conflict. A general behavioural nudge helps
`sequential_0`, `sequential_1`, `sequential_2`, `rpc_districts_in_bbox_limits` and a SAM evidence
case, one case each, and does nothing in particular on any single one of them. Attributing a lesson
against the case it came from is therefore biased against exactly the advice most likely to be worth
keeping.

The same question was asked three ways, and the first two answers were wrong for instructive reasons.
On a single case that already passed 5/5 with no rule at all, all three placements scored 5/5, which
says nothing: a saturated case cannot detect anything. Repeating it on a case that genuinely fails,
the on-demand channel was confirmed to be delivered, since `benthic_playbook` was called 4/4 and the
case still failed, so the channel is not the blocker either. Resolving it at suite level is the
measurement quoted above.

The consequence for the loop is a limit, not a fix: per-case attribution can promote case-specific
lessons, and needs a suite-level measurement to promote general ones. A suite-level A/B costs two
full runs, so it is not something to do per lesson per round. Nothing in the loop claims the ability
to discover a general rule on its own, and the one general rule that measurably works is hand-written.

### What the numbers can and cannot say

Holdout cases are excluded from reflection, so the round trend describes cases the playbook was not
built from, and the guard is their only reader. An earlier version reflected on all 33 cases and
scored all 33, which made the headline a training number; three of the twelve lessons in the
accumulated playbook were extracted from holdout cases as a result.

Neither the round trend nor the tripwire can resolve the effect sizes this loop produces. Thirty-three
cases at 84% move by one or two cases between rounds for no reason. `eval/attrib.py` exists for
that: it removes one lesson's advice from the served playbook and runs the case it came from with and
without it, alternating repetitions so server drift hits both arms equally.

```sh
python eval/attrib.py --lesson-id 7462c096217e46cc \
  --case join_usaspending_all_entities_usp_cl_legislator_terms_evidence \
  --playbook eval/harness/cache/playbook.json --reps 5
```

Two details make its verdict mean something. The "without" arm is a real removal: the lesson is
dropped from the document and any core line it was distilled into is dropped with it, so the advice
is not still present in the always-on slice. And a sibling case can be added with `--sibling` to
catch a lesson that merely restates the answer. Repetitions are interleaved, and the per-case counts
are the result; the verdict is a label over them and carries no weight on a case that flips between
repetitions on its own. A sub-threshold difference at five reps is always inconclusive, because any
non-zero difference leaves one arm mixing.

`eval/golden/questions.json` is the harness's check on itself: four hand-verified cases drawn from
the signed catalog and live calls, which must pass with the seed playbook alone. A failure there is
a bug in the harness, the scorer, or the tool surface, and is never a result about the playbook. The
generator refuses to write into that directory, and `tests/test_golden.py` re-checks the expected
values against the signed catalog so a catalog change cannot leave the suite quietly stale.

If the signed catalog changes after a playbook was generated, the fingerprint no longer
matches and the server serves the static core plus a staleness notice instead of stale dataset
detail, until the candidate is re-consolidated and re-gated.

The API does not expose server-side grouped aggregates. Large calendar-year scans can exceed the complete-scan or upstream timeout limit; the service reports that limitation rather than returning partial totals. Narrow the date range or use a signed pre-aggregated relation when available.

The evaluator is independent of the MCP query implementation: it uses direct signed PostgREST and RPC probes for expected values. Use `--case-filter` and `--limit` for focused iteration, and retain a full run as the regression gate.
