# benthic-mcp: working memory

## The goal
A mechanism that works and gives accurate results. The original goal was an MCP server that improves
its own guidance; the evidence says that specific loop cannot work, and what replaced it is recorded
below.

## Current state (2026-09-29)

**Repo:** `github.com/benthic-io/benthic-mcp`, public, MIT, 24 commits, CI green, 394 tests.
llama-server is on port 8081, benthic-mcp HTTP on 8082. Both registered in
`~/.config/systemd/user/`. MCP config at `~/.config/llama.cpp/mcp-servers.json`.

**Headline, measured over 2 reps at 6 turns with thinking off:** 32/33 both reps, holdout 8/8 and
7/8, 31 of 33 cases passing both reps. Golden 8/8. This is a count of cases answered correctly, NOT a
measurement - the suite's MDE is larger than the whole remaining failure set.

## The three findings that changed the design

**1. The statistical gate could never confirm anything.** MDE 7.81 cases; total headroom for any
guidance is 4.5 cases. A gate with an MDE larger than the maximum achievable effect can only detect
harm, which is why it kept reporting that nothing worked. Do not build on suite-level A/B for
small effects. `llama-server` runs with `-np 1`, so concurrency buys 1.00x (measured, not assumed).

**2. The prose self-improvement loop cannot express a code change, and no claim about it was ever
measured.** The reflector produced exactly 8 distinct lessons over 6 rounds (48 reflections
attempted, 30 recorded, merging to 8). Measured against the always-on core by token overlap, **3 of
8** restate it - not 8 of 8. The earlier figure was written down before the store recorded which
channel a lesson arrived by, so it could not be checked either way. The mechanism behind it is real
and verified: `eval/reflect.py:build_prompt` never shows the reflector the core, and the pipeline
deletes any sentence naming a non-manifest identifier, so "add a type coercion" has nowhere to go.

What actually blocks the loop is colder than restatement: **0 of 12 lessons had ever been
attributed.** `source_case()` reads `record.source_ref`, the field was added after all 12 were
written, and `classify()` therefore had never run. Provenance was reconstructed from
`eval/harness/rounds.json`, which logs `lesson_id` and `case_id` per reflection: 8 reflector lessons
now carry a source case, of which 2 are on holdout and correctly refused, leaving **6 measurable**.
The 4 `benthic_report` lessons match the 4 report calls across the same rounds exactly, so the
8 + 4 = 12 split is arithmetic, not inference. Every change that ever worked here was a code change.
Struggle detection reflected on 62.5% of passing cases and never once selected the 0/94 case.

**3. The scorer checked the route, not the result.** It read 6 of 22 expected-value fields, so a
wrong answer delivered confidently along the right path passed. This hid three real defects.

## Real defects found (all invisible to the model-in-the-loop apparatus)

- A signed join between `congressional_district` (string, `'03'`) and `legislator_terms.district`
  (integer, `3`) compared them in Python, matched nothing, and returned 0 rows where 127 exist.
  55 of 55 observed calls. The two cases scored as PASSES for 51 stored runs. A `benthic_report` had
  diagnosed it correctly while the harness could not see it. Fixed by `_type_coercion` /
  `_coerce_token` in `src/benthic_mcp/joins.py`.
- `context_conditions` columns were parsed AFTER the `select` lists were built, so no partial signed
  join could ever match a row, for any input. Fixed in `src/benthic_mcp/query.py`.
- `number` was missing from the numeric type set. It is the catalog's own name for a decimal and the
  second most common declaration in the manifest (427 of 3419 columns).
- `order=` raises a bare `TypeError` on mixed-type columns, not caught by the tool wrapper. Still
  open.

## What replaced the gate

- **Contracts:** `tests/test_contracts_catalog.py`, 26 falsifiable properties over catalog and joins,
  no model, no network. Five arrived violated and are now fixed. Zero xfails remain.
- **Answer checking:** the scorer now reads `right_count`, distinct `right_keys` and signed
  `reliability` off the server's structured output. Rescoring 607 stored case-runs turned 51 red
  (server was wrong) and 8 green (suite was wrong).
- **Left-join counting:** a left join keeps every left row with a null right, so "the right side is
  empty" returns 1 row. Counting returned rows made a correct answer read as wrong.

## Case suite: two cases were unanswerable

`multi_step_0_0` and `multi_step_0_1` asked the agent to walk a chain whose first hop is provably
empty (`samer.sam_registrations` has no row for `uei=ESELKUJSAM45`). The scorer demanded two
successful hops, so a truthful dead end was a failure. They now ask where the chain terminates and
pass 4/4. Marked `unanswerable: true` in `eval/generated/questions.json`.

Still failing: the `relation_trap` pair, which asks for an underspecified office and date. The scorer
has no way to reward asking for the missing parameter.

## Rules learned the hard way

- **A null from an instrument that discards data is not evidence of absence.** I removed the
  answer-delivery core rule on a 2-rep A/B that read 23/50 both arms; `attribute_suite` keyed results
  by case instead of case-and-repetition, so half the data was thrown away. Over all reps it reads
  44/50 vs 46/50, which clears `min_delta` and reads `fixes`. The rule was restored. Repetitions are
  the only thing that buys power in this instrument.
- **Check the exit code's *meaning*, not that it passed.** I ran local `ruff check` after already
  having edited the offending file, so a lint error introduced in `bb18167` sat through three commits.
  CI caught it.
- **Run the gate against the real data early.** Four holes in the attribution gate were found by
  running it, not reading it: `untested()` only looked at pending, consolidation was
  self-perpetuating, `promote_candidate` resurrected quarantined lessons, and the arm under test was
  built by removing a lesson that is correctly absent from the document.
- **One delivery problem can look like many.** `detail='full'` once turned 1 discover call into 3 and
  blew a 5-turn budget.
- **Verify second-hand claims.** The handoff notes on the MCP config were half right: the file was not
  malformed, it was the Web UI config passed to llama-server's stdio-only flag.

## Next steps, in order

1. `order=` TypeError on mixed-type columns - the last contract-10 violation, still open.
2. Delete the reflector-to-prose path, or repoint it at code changes with a human reviewer. Do not
   call it a loop; it is a triage queue.
3. `relation_trap` cases: fix or retire on principle, not to make numbers look better.
4. Re-scope the suite to the ~9 cases that discriminate server capability; the rest measure the
   model's turn discipline.

## Files worth reading first

- `src/benthic_mcp/joins.py` - `_type_coercion`, `_coerce_token`, `_join_key`
- `src/benthic_mcp/query.py` - `build_single_join` (context columns), `_validate_output_columns`
- `eval/run_eval.py` - `_join_answer_ok`, `_matched_row_count`, `_count_matches`
- `tests/test_contracts_catalog.py` - the 26 contracts
- `docs/findings.md` - the research log, including the wrong numbers next to the right ones
