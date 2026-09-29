# benthic-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over signed [Benthic Data
Provenance](https://benthic.io/bdp/) datasets, plus the evaluation harness that measures whether the
playbook it serves actually improves an agent's answers.

The server exposes four tools:

| tool | what it does |
| --- | --- |
| `benthic_discover` | finds the smallest relevant signed relation, its columns, and its join paths |
| `benthic_query` | runs a bounded single-relation query with compact filter, aggregate, and `having` expressions |
| `benthic_join` | runs a multi-relation plan using only signed BDP join paths |
| `benthic_rpc` | runs three allowlisted spatial RPCs |

Plain-language questions are interpreted by the calling chat model, which turns a question into compact
tool arguments. This service validates and executes those arguments without embedding a model of its
own, so what an agent is *allowed* to ask is decided by a signed manifest rather than by a prompt.

## Why the trust model is the point

The catalog is signed. The server pins an Ed25519 authority key, verifies RFC 8785 canonical JSON,
SHA-256 payload hashes, and every member manifest against the collection hash, and then serves only
what the signature covers:

- only relations marked `queryable: true` in a signed manifest
- only join paths present in the signed join graph
- only an allowlist of RPC endpoints, hand-written in `src/benthic_mcp/catalog.py`
- no arbitrary URLs, relations, SQL, or write operations
- API and RPC traffic restricted to HTTPS endpoints on `benthic.io`

The playbook this server serves to the model is prose, and prose is model-authored. It can therefore
never widen any of the above. A fact the signed catalog already carries is always read from the
verified manifest at serve time, so guidance cannot contradict it. See
[`SECURITY.md`](SECURITY.md) for the trust boundary in one page.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Network access to `https://benthic.io`
- A local [`llama-server`](https://github.com/ggml-org/llama.cpp) for the agent that calls the tools

```sh
git clone https://github.com/benthic-io/benthic-mcp
cd benthic-mcp
uv sync --frozen
```

## Running it

Two transports are supported, and they are not interchangeable with the two MCP clients in llama.cpp:

| client | configured by | format | transport |
| --- | --- | --- | --- |
| llama-server | `--mcp-servers-config` | object with `mcpServers` | stdio only |
| llama.cpp Web UI | its MCP settings dialog | array of server objects | HTTP and SSE |

Set `BENTHIC_MCP_TRANSPORT=stdio` to serve over stdio, which is what llama-server's own client needs.
A cold spawn is about two seconds. `config/llama-mcp.README.md` has both config formats, the accepted
schema, and the two ways to fail silently; `config/llama-mcp-servers.example.json` and
`config/llama-mcp.example.json` are the two files.

For the HTTP service, which the Web UI uses and which needs a URL and a token because a browser cannot
spawn a subprocess:

```sh
mkdir -p ~/.config/benthic-mcp
cp config/benthic-mcp.env.example ~/.config/benthic-mcp/env
chmod 600 ~/.config/benthic-mcp/env
# Replace the token placeholder, and set MCP_HOST / LLAMA_HOST to your own hosts:
openssl rand -base64 36
```

```sh
scripts/start-benthic-mcp.sh
```

The service listens on port 8082 by default and exposes the Streamable HTTP MCP provider at
`http://<mcp-host>:8082/mcp`. `/health` requires the same bearer token.

`examples/` holds two starting points that need editing for your machine:
`start-llama-server.sh` is one tuned `llama-server` configuration (the one the numbers below were
measured under), and `benthic-mcp.service` is a user-level systemd unit.

**Set `enable_thinking=false` on the calling model.** It cuts completion tokens by about 64% and makes
the result reproducible, which is a deployment instruction for whatever calls the MCP rather than a
server setting. Its effect on the pass rate is about one case and is not established.
`docs/findings.md` records that asking the model in a system prompt not to deliberate does not work.

## The playbook

`benthic_playbook` returns dataset-specific instructions so the agent does not rediscover the schema
each session. Three modes, set by `BENTHIC_PLAYBOOK_MODE`:

- `off` - no playbook; the tool reports that it is disabled
- `seed` (default) - the curated seed in `src/benthic_mcp/seed.py`
- `active` - the promoted playbook, falling back to the seed if the file is missing

An agent can also report a mistake it made with `benthic_report`. **A reported lesson is never served
until it has been measured to help.** Accumulation used to promote on catalog verification alone,
which proves a statement is true and says nothing about whether it changes behaviour; twelve
accumulated lessons were later measured and none earned a place, while one made a case measurably
worse. Lessons are now attributed against the case they came from before they can be served, and a
regression is quarantined. The measurement, the dead ends, and the limits of the instrument are in
[`docs/findings.md`](docs/findings.md).

## What has been measured

33 generated cases against a 35B MoE coder model, one repetition per case, 5-turn budget. The suite
is small and noisy: it moves by one or two cases between identical runs.

| configuration | pass rate | completion tokens |
| --- | --- | --- |
| seed, thinking enabled | 29/33 then 27/33 | 53,231 / 56,917 |
| seed, `enable_thinking=false` | 29/33 then 29/33 | 19,092 / 20,226 |

Two repetitions of all 33 cases each, same seed, same turn budget. Held-out cases are excluded from
reflection, so the headline is not a training number.

The two effects are not the same size and the table separates them deliberately. Disabling thinking cuts
completion tokens by about 64%, and nothing in the run-to-run spread comes close to that, so it is not
in question. Its effect on the pass rate is about one case, and one case is what thinking-enabled
repetitions disagree with each other by, so **the accuracy benefit is not established** and the README
does not claim it. What is visible is that thinking-off is reproducible where thinking-on is not:
29/33 twice, against 29/33 then 27/33.

A third row is worth stating plainly. The accumulated lessons were served and measured, and **none of
the twelve earned a place**; one was measured as actively harmful and quarantined. The single
hand-written core line that the project had carried longest was removed after a paired A/B over 100
case-runs per arm found no effect. Details in [`docs/findings.md`](docs/findings.md).

## Evaluation

```sh
uv run python eval/generate_cases.py                 # rebuild the suite from the signed catalog
uv run python eval/run_eval.py                       # run it
uv run python eval/run_eval.py --case-filter multi_step --reps 5
```

Each run writes the cases, model transcript, tool arguments and results, token usage, timings, and a
report under `eval/runs/<run-id>/`. Expected values come from direct signed PostgREST and RPC probes,
so the evaluator does not depend on the MCP query implementation it is scoring.

Four hand-verified cases in `eval/golden/questions.json` must pass with the seed playbook alone. They
are a check on the harness, the scorer, and the tool surface, so a failure there is a bug rather than a
result. `tests/test_golden.py` re-checks their expected values against the signed catalog, so a catalog
change cannot leave the suite quietly stale. Run it at `--max-turns 6`, not the 5 the generated suite
uses: one case needs six to test what it is for and was failing about one run in five at five, which is
a tripwire firing on a capability it is not watching. At six it is 12/12.

Long runs belong in tmux so they can be watched:

```sh
scripts/tmux-run.sh my-run logs/run.log uv run python eval/run_eval.py
scripts/tmux-ls.sh
```

Two attribution tools sit above the suite, and they answer different questions:

```sh
# Does this one lesson fix the case it came from? Cheap, but biased against general advice.
uv run python eval/attrib.py --lesson-id <id> --case <case-id> --playbook eval/harness/cache/playbook.json

# Does this rule earn a place in the always-on core? Two full runs of the tuning split.
uv run python eval/attribute_suite.py --rule "Never end the turn without a final answer." --reps 1
```

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
| `BENTHIC_MCP_ALLOWED_HOSTS` | loopback only | Accepted Host headers |
| `BENTHIC_MCP_ALLOWED_ORIGINS` | loopback only | Accepted browser origins |
| `BENTHIC_MCP_TRANSPORT` | `http` | `http` or `stdio`; stdio is what llama-server's MCP client needs |
| `BENTHIC_MCP_BEARER_TOKEN` | unset | Required for the HTTP transport only |
| `BENTHIC_PLAYBOOK_MODE` | `seed` | `off`, `seed`, or `active` |
| `BENTHIC_PLAYBOOK_PATH` | `<cache>/playbook.json` | Promoted playbook location |
| `BENTHIC_PLAYBOOK_TOKEN_BUDGET` | `600` | Token cap for the always-on core slice |
| `BENTHIC_TRACE_ENABLED` | `1` | Record objective tool-call traces |
| `BENTHIC_TRACE_RETENTION_DAYS` | `30` | Trace retention |
| `BENTHIC_LESSON_RETENTION_DAYS` | `30` | Lesson retention |
| `BENTHIC_TRACE_INCLUDE_TEXT` | `0` | Store question text from `benthic_report` |
| `BENTHIC_CONSOLIDATOR_LLM_URL` | local llama-server | Model used by the consolidator |

The shipped allow-lists name loopback only, deliberately: they gate the `Host` and `Origin` headers,
so a default that named a particular machine would silently authorise that host for anyone who
installed the server without reading the configuration.

The fetched `keys.json` is not used as a trust root. Add rotated trusted keys through
`BENTHIC_TRUSTED_KEYS` only after out-of-band verification.

The API does not expose server-side grouped aggregates. Large calendar-year scans can exceed the
complete-scan or upstream timeout limit; the service reports that limitation rather than returning
partial totals. Narrow the date range or use a signed pre-aggregated relation when available.

If the signed catalog changes after a playbook was generated, the fingerprint no longer matches and the
server serves the static core plus a staleness notice instead of stale dataset detail, until the
candidate is re-consolidated and re-gated.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -m "not live"
```

The non-live marker excludes tests that need a real BDP endpoint and a running `llama-server`:

```sh
BENTHIC_LIVE_TESTS=1 uv run pytest -m live
BENTHIC_LIVE_TESTS=1 BENTHIC_LIVE_REGRESSION=1 uv run pytest -m live
```

`tests/test_loop_invariants.py` is worth reading before changing the improvement loop. It states the
properties that make a self-modifying system trustworthy - nothing is served without a measured
effect, a verdict is auditable, the document cannot self-perpetuate - rather than examples of them.

Long-running changes to the playbook belong under `eval/harness*/` with `BENTHIC_CACHE_DIR` pointed
somewhere disposable, so a run never touches the live store:

```sh
BENTHIC_CACHE_DIR=eval/harness/cache uv run python eval/harness.py --rounds 6
```

## Documentation

- [`docs/findings.md`](docs/findings.md) - what the harness measured, what did not work, and the limits
  of the measurement
- [`SECURITY.md`](SECURITY.md) - reporting a vulnerability, deployment notes, and the trust boundary

## License

MIT. See [`LICENSE`](LICENSE).
