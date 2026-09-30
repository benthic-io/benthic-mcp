# Driving an OpenCode agent from a shell timer

Verified against the running server and the installed CLI (`opencode v2.0.20`), not from
documentation alone. Source of truth is <https://opencode.ai/v2/docs/>; where behaviour was
confirmed only by running it, that is said.

## The four things that matter

```bash
# 1. Create a session, once, and reuse it. Prints the session id.
SID=$(opencode api post /api/session --data '{"title":"benthic-observe"}' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["id"])')

# 2. Send a task into it. --auto auto-approves permissions; without it an unattended
#    invocation HANGS on the first permission prompt rather than failing.
opencode run --session "$SID" --auto --format json "$(cat task.md)"

# 3. Continue the same session later - this is what makes a timer loop coherent.
echo "next instruction" | opencode run --session "$SID" --auto

# 4. Block until the agent goes idle. Better than polling for a completion marker,
#    which is how the previous waiter timed out against a cycle that was working fine.
opencode api post /api/experimental/session/$SID/wait
```

**`opencode run` is the headless mode.** There is no `--headless` and no `--print`; the docs
describe `run` as "designed for scripts, CI jobs, and other workflows".

## What is verified, and how

| Behaviour | How verified | Confidence |
| --- | --- | --- |
| `opencode run` runs a task non-interactively | ran it, got `HEADLESS_OK` | high, documented |
| `--format json` emits newline-delimited events | ran it, parsed `step_start` / `text` | high, documented |
| `--auto` auto-approves permissions | present in `--help`, **absent from the docs page** | high, undocumented |
| `--session <id>` continues an existing session | created one, two turns, both answered | high, documented |
| stdin is read when not a TTY | piped a prompt, got `CONTINUED` | high, **not in the docs** |
| `POST /api/experimental/session/{id}/wait` | confirmed in the live OpenAPI | high |

Two claims in this project have been recorded as bugs when they were only undocumented. Test them
before relying on them: `--auto` and stdin.

## What does not exist

- **No scheduler, job queue, or workflow engine**, in the product or in the plugin API. The closest
  primitives are `ctx.storage` plus `setInterval` inside a plugin's `setup`. Build the scheduler
  externally.
- **No Python client.** `@opencode/client` and `@opencode/sdk` are the real packages;
  `@opencode-ai/sdk` is pinned at V1 and must not be used. The PyPI Python packages are third-party
  and mostly V1-era. Generate a client from `/openapi.json` if a Python one is needed.
- **No documented concurrency ceiling for subagents.** Do not assume one.
- `agents.request` is preserved but **not sent** with model requests in V2.

## Subagents

Custom agents are markdown at `.opencode/agents/<name>.md` with frontmatter for `description`,
`mode: subagent`, `model`, `system`, `permissions` (ordered, last match wins) and `steps`. The
`subagent` tool takes an agent id, a description and a complete prompt; `background: true`
notifies the parent on completion and returns a session id that resumes the same child.

`subagent_depth` defaults to 1: a primary agent may launch subagents, and those subagents may not
launch further ones. Raise it deliberately - an unbounded tree of agents editing one checkout is
how a swarm produces conflicting changes rather than leverage.

## Third-party projects

Unendorsed by OpenCode; existence and stated purpose only.

- `ZaxbyHub/opencode-swarm` - an OpenCode plugin. Separate implement / review / test agents with
  gated execution: nothing ships until required gates pass. Closest match to this project's
  "every change carries a test that fails without it" rule.
- `jonwiggins/optio` - a control plane for scheduled sessions across CLIs, OpenCode included.
  Treat as inspiration rather than a dependency.
- `nbardy/unleashd` - swarms as background jobs with steerable artifacts.

## How this is used here

`scripts/observe-once.sh` runs a cycle: probe the live server, survey what the model got wrong,
diff against the baseline. It does not edit anything.

The agent step is separate and deliberate. Deciding whether a failure is the server's fault or the
model's is judgement, and an unattended loop that guesses would manufacture false greens - which has
happened here seven times. So: the loop observes and reports, and a headless agent session takes
it from there, one problem at a time, each change carrying a contract proven to fail without it.