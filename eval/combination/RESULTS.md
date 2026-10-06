# NGOpen combination suite: the first clean run

Run `run-20261004T2211`, at `--max-turns 12`, against `llama-server` on `-ncmoe 31`. This
supersedes `run-20260930T2352`, of which **58 of 100 cases ran against a wedged server** and whose
numbers are void; that run's transcripts have since been deleted as superseded, and the record of
what it showed is the paragraph above. `grow` and `now` here are still not scores - they have no
`expected` field - but they are at least measurements.

```json
{
  "run": "run-20261004T2211",
  "supersedes": "run-20260930T2352",
  "max_turns": 12,
  "headline": {
    "cases": 100,
    "answered": 87,
    "broken_calls": 0,
    "contamination": "none: no case reached the timeout with zero tool calls",
    "answered_by_tag": {
      "grow": { "n": 79, "answered": 67, "server_refused": 41, "previous_wedged": "77%" },
      "now":  { "n": 14, "answered": 13, "server_refused": 5,  "previous_wedged": "86%" },
      "refuse": { "n": 7, "answered": 7, "server_refused": 2, "previous_wedged": "2/7" }
    }
  },
  "the_thirteen_failures": {
    "server_refused": 8,
    "never_issued_a_query": 6,
    "overlap": 1,
    "note": "A server refusal is correct behaviour on an unanswerable question. The defect is the six that never queried."
  },
  "the_six_defects": {
    "shared_shape": "called benthic_discover and benthic_playbook, then stopped. No benthic_query.",
    "cases": [
      "sam-naics", "geo-district-split", "p527-me01",
      "time-four-clocks", "prog-aln-subsection", "agg-ein-by-state"
    ],
    "truncation_signature": 5,
    "truncation_evidence": "longest reasoning 30,663-33,157 chars, i.e. the 8,000-token ceiling",
    "the_sixth": "prog-aln-subsection stopped after 1,617 chars - a different failure, it gave up early"
  },
  "token_budget_ladder": {
    "case": "time-four-clocks",
    "note": "The two knobs are coupled. At ~36 t/s anything above ~10,800 tokens outruns a 300s request timeout.",
    "runs": [
      { "max_tokens": 8000,  "timeout": 300,  "truncated": true,  "answered": false, "outcome": "cut mid-thought" },
      { "max_tokens": 16000, "timeout": 900,  "truncated": true,  "answered": false, "outcome": "still cut" },
      { "max_tokens": 24000, "timeout": 300,  "truncated": false, "answered": false, "outcome": "TimeoutError" },
      { "max_tokens": 32000, "timeout": 1200, "truncated": false, "answered": false, "outcome": "TimeoutError at 1,215s" }
    ],
    "conclusion": "Not fixable by budget. More tokens buy more deliberation, not action."
  },
  "budget_experiment": {
    "run": "run-budget24",
    "design": "4 cases at 24000 tokens / 900s, against the same 4 at 8000 / 300s. Decision rule fixed before the run: >=3 of 4 answering would justify raising it suite-wide; 0-1 would mean guidance is the only lever.",
    "cases": [
      { "id": "sam-naics", "baseline": "334s no answer", "raised": "953s no answer", "note": "never queried either way" },
      { "id": "geo-district-split", "baseline": "401s no answer", "raised": "629s no answer", "note": "queried only in the raised run, still no answer" },
      { "id": "p527-me01", "baseline": "380s no answer", "raised": "1061s no answer", "note": "never queried either way" },
      { "id": "agg-ein-by-state", "baseline": "402s no answer", "raised": "1400s no answer", "note": "never queried either way" }
    ],
    "truncation_fixed": "4 of 4",
    "answered": "0 of 4",
    "cost": "1517s -> 4044s (2.7x)",
    "verdict": "The budget change works mechanically and buys nothing. Do not raise it suite-wide."
  },
  "missing_signed_hops": {
    "count": 8,
    "cases_that_still_answered": 8,
    "blocked_outright": 0,
    "note": "Every one names the gap and gives what it can. summary.json's missing_hops is a wish list, not a defect list."
  },
  "refusal_shape": {
    "flagged": "4 of 7",
    "verdict": "not 4 violations",
    "note": "Read by hand: no-write is a textbook capability refusal, no-lobby and no-causal are clarification requests followed by a data map. The summary wording asserted a verdict the module docstring explicitly disclaims; that is fixed. Detection is deliberately unchanged."
  }
}
```

## Two cases answered with zero tool calls, both correctly

`no-causal` (21.6s) and `id-name-zip` (25.7s). Neither needed a call: `no-causal` correctly
declines until a member is named, and `id-name-zip` answers about the *shape* of the join - UEI
exact from usaspending to SAM, no shared key to BMF so name+ZIP is heuristic, EIN exact from SAM -
which is in the always-on catalog description. Counting `tools_called == 0` as suspicious would
flag both.

## Per-case judgement, corrected against the clean run

- `id-name-zip` - answered correctly without a call, as above.
- `id-pre-uei` - refused, 19 tool calls. DUNS to UEI through the crosswalk is a query, not a signed join. Correct.
- `flow-maine-990` - answered by naming the gap; 990 XML to BMF on EIN is same-dataset and unsigned. Correct.
- `flow-pub78` - answered. pub78 eligibility needs `bmf_organizations` to `pub78_eligible`, unsigned, and it said so.
- `flow-four-hops` - answered on 4 tool calls. Every hop past the first is the question.
- `cmte-me02` - answered by refusal; `legislator_terms` to `committee_membership` is unsigned.
- `p527-awards` - answered by refusal; `political_orgs_527` to `bmf_organizations` is unsigned.
- **`no-causal` - the old annotation was wrong.** It read as "refused but for an incidental
  reason, then supplies the framework for the causal analysis the case forbids... the failure mode
  nothing currently detects." Re-read in full, it asks which member, which is a genuinely missing
  input, and the steps it lists are descriptive retrieval rather than a causal method. It is a
  correct refusal. This is the false positive `refusal_shape.py` produced.
- `no-full-extract` - answered by refusal, naming the real blocker. Correct diagnosis, right direction.

## What is not measured here

- **`grow` and `now` are not scores.** The suite carries no `expected`, so `classify.py` refuses to
  score it. "Answered" means the model produced an answer, not a right one.
- **Latency is unmeasured per call.** `cases.jsonl` records tool *names* with no durations, so the
  43 cases over 295s cannot be split into slow queries and slow thinking.
- **`refuse` at 7/7 answered is not 7 correct refusals.** Only 3 refused and stopped cleanly.
