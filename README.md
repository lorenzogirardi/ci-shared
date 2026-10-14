# ci-shared

Reusable GitHub Actions workflows shared across lorenzogirardi's repos, so the
AI-review/analysis logic and its scripts live in one place instead of being
copy-pasted per repo (it already was, byte-for-byte, between `flask-test-api`
and `cloudflare-free-exporter`).

Uses the same OpenAI-compatible client (`scripts/openrouter_ai.py`, default
endpoint OpenCode Zen, works with any `OPENROUTER_ENDPOINT`/`OPENROUTER_MODEL`
including OpenRouter) that consumer repos already had. No Claude API, no
Claude Code routines — plain HTTP call from a stdlib-only Python script.
The one exception is the agent engine (`scripts/agent_pipeline.py`), a
langgraph `StateGraph` — see `requirements-autofix.txt`, installed only when a
caller enables a feature that uses it (autofix, structured review, the agent
workflows).

Full design rationale, diagrams, and the story behind each non-obvious
decision live in [`docs/architecture.md`](docs/architecture.md). This file is
the quick reference.

## Versioning

Consumers pin a tag (`@v1`), never `@main`, so a change here can't silently
break every consumer at once. Each reusable workflow checks out `scripts/`
with a **literal** `ref: v1` in its own "Checkout shared scripts" step — not
resolved dynamically. `github.workflow_ref` inside a called reusable workflow
reflects the *caller's* ref (observed: a consumer PR's merge ref), not the
tag pinned in this repo's own `uses:` line — there is no context that
reflects that. Because the checkout step's `ref: v1` ships as part of the tag's
own content, bumping the tag and bumping that literal happen in the same
commit by construction — they cannot drift apart.

A behavior change moves the `v1` tag forward
(`git tag -f v1 && git push -f origin v1`) once verified against the real
consumers; a breaking change instead gets a new `v2` tag (and its own
`ref: v2` literal) so existing `@v1` consumers are unaffected until they bump
on purpose.

`v3` is not moved by hand. `release-tag.yml` moves it to a commit of `main`,
forwards only, after two things: `Test scripts` has passed on that exact
commit, and the consumer's canary has passed against it. For the second, the
commit is published as `v3-next` and the canary of the repository named in the
variable `ENGINE_GATE_REPO` is started in candidate mode: its pull requests
target a base branch named `canary/next-*`, and for those the agent workflows
check out the scripts and prompts at `v3-next`. If a scenario does not hold,
`v3` stays where it is and the run is red. Until `ENGINE_GATE_REPO` is set
(`owner/repo`; `ENGINE_GATE_WORKFLOW` names the canary workflow, default
`pipeline-canary.yml`) that second step is skipped and the tag moves after the
tests alone; a manual run of `release-tag.yml` with the `consumer` input checks
that the gate can reach a given repository. Unit tests do not see what a model
does with a real pull request; this does. A consumer whose workflow grants fewer
permissions than a reusable workflow asks for does not start at all: when a
change needs a new permission, update the callers first, then merge here.

## v3: role-based agents, the architect first

`v3` is the tag that moves. It carries the agents below on top of everything `v1`
does, with the graph that enters at the architect. `v2` (the same agents, frozen at
the first commit of that graph) and `v1` stay where they are for repos that have not
moved. All reusable workflows in `v3` check out `ref: v3`.

| Workflow | Roles | Writes? |
|---|---|---|
| `reusable_agent-pipeline.yml` | planner -> writer -> deterministic checks -> developer (correctness) + security + SRE + architect -> dedup/validation -> one fix asked by the developer (correctness), reread by it -> documentation reviewer -> changelog | opens a PR certified at its head commit; retried once with twice the budget, then abandoned (commented, nothing pushed); never merges itself |
| `reusable_agent-change.yml` | the engine on a change that already exists: `mode: pr` (any pull request), `mode: ci` (one whose checks failed) or `mode: push` (a push straight to the default branch). The architect reads the code and writes the brief -> developer 1 -> checks -> developer 2 (accepts or sends back, three times at most, then the architect decides) -> QA -> SRE and security side by side (they inform) -> documentation -> the architect closes. The record of the run (`agents-run/`) is in the job's artifact | fix commits on the PR branch, or a PR from `agent/push-<sha>`; never merges; ignores its own commits |
| `reusable_agent-review.yml` | the four reviewers on any PR diff | one comment |
| `reusable_agent-merge.yml` | none (deterministic): the merge gate | squash-merges a pull request whose head commit is certified and whose required checks succeeded on that same commit; only into its own base branch; stops at the circuit breaker |
| `reusable_agent-main-guard.yml` | none (deterministic): the guard on the base branch | re-runs the failed jobs once, then reverts to the last green state and says so on the pull request the change came from (label `agent-reverted`). Opens no issue |
| `reusable_pipeline-health.yml` | none (deterministic): the loop checks itself | starts the build for a commit that has none; abandons a pull request that has no verdict and nothing left to run; goes red on a certification given while an agent could not do its job, and on an agent workflow that is failing now |
| `reusable_pipeline-canary.yml` | none of its own: sends known changes through the real loop | opens the scenarios of the consumer's `.github/canary.json` as pull requests against a throwaway copy of the base branch, checks the outcome (verdict, required checks, files changed, contents, and what the agents' report says), closes them. Never merges |
| `reusable_changelog.yml` | none (deterministic) | one `docs(changelog)` commit per push to the default branch |
| `reusable_docs-architect.yml` | documentation architect (Diataxis) | **nothing**: a proposal, optionally as an issue |

How it fits together:

- Roles are prompts in `prompts/agents/`; the code that acts on their output
  (`scripts/agent_lib.py`) is strict: findings need severity, file, line,
  evidence and fix, and are dropped unless the line is inside a changed hunk;
  writer changes are capped, unique-anchored, all-or-nothing and never touch
  `.github/workflows/`.
- Deterministic checks run in a process whose environment contains no model
  key and no token, from a checkout that keeps no credentials. Only the final
  `publish` step holds a write token and it runs no repository code.
- One loop: only the developer who reviews correctness sends a change back, once, and rereads the
  fix; the other roles inform. Failing checks are retried within `max_verify_retries` and the writer
  within `writer_rounds`. When a run stops without converging, the PR says so.
- Cost per role is recorded (`.ai/agents/*.usage.json`) and shown in the PR.
- Every change ends merged, abandoned or reverted; none waits for a person.
  The rules that make that safe are code, not prompt: tests are the
  specification (a test is changed only when the change's own description says
  so, quoted word for word); a test an agent has just written is not the
  specification, so the application is never changed to satisfy it; without a
  usable answer from the correctness reviewer, the test steward or the
  documentation reviewer nothing is certified; a verdict is
  bound to one commit, and a run whose commit is no longer the head publishes
  nothing; documentation edits never touch `CLAUDE.md`, `AGENTS.md` or
  `.claude/`.
- With nobody in the loop, nobody notices when a piece of it silently stops.
  `reusable_pipeline-health.yml` (every 30 minutes) and
  `reusable_pipeline-canary.yml` (every night) are what notices. Wire both.

## Workflows

### `reusable_pr-diff-review.yml`

Diffs `base...head` of a PR, sends it to the model, posts/updates a single PR
comment. Job permissions: `contents: read`, `pull-requests: write` — safe to
call on arbitrary/human/fork PRs.

Key inputs: `system_prompt_extra` (project-specific focus), `comment_marker` /
`comment_heading` (so two callers in one repo don't fight over the same
comment), `openrouter_*` (forward your repo vars — `workflow_call` does not
inherit `vars.*` automatically). If the consumer repo has a
`.github/ai-review-rules.md` file, it's read and appended to the prompt
automatically.

Secret: `openrouter_api_key` (optional — empty means "skip the model call,
post a did-not-run comment"; used to blank the key on fork PRs).

Output: `clean` — `"true"` only if the review ran and its **exact last line**
was `VERDICT: CLEAN` (not a substring match — see architecture.md for why
that distinction is load-bearing).

### `reusable_pr-review-sweep.yml` (removed)

The scheduled sweep over a dependency bot's pull requests (review, triage, repair, merge) was a second
engine. The graph does all of it since `v3`: the bot's pull requests are reviewed, repaired and merged
by `reusable_agent-change.yml` and `reusable_agent-merge.yml`, like any other. The workflow and its
script are still there at the frozen tags `v1` and `v2`, for a consumer that has not moved.

## Consumer wrapper examples

Plain review (safe for any PR, including forks):

```yaml
jobs:
  review:
    if: vars.AI_ENABLED == 'true' && github.actor != 'renovate[bot]'
    uses: lorenzogirardi/ci-shared/.github/workflows/reusable_pr-diff-review.yml@v1
    with:
      openrouter_model: ${{ vars.OPENROUTER_MODEL }}
      openrouter_endpoint: ${{ vars.OPENROUTER_ENDPOINT }}
      openrouter_site_url: ${{ vars.OPENROUTER_SITE_URL }}
      openrouter_app_name: ${{ vars.OPENROUTER_APP_NAME }}
    secrets:
      openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

See `flask-test-api/.github/workflows/` for the real wrappers: `agent-change.yml`,
`agent-ci-failure.yml`, `agent-merge.yml`, `agent-main-guard.yml`,
`pipeline-health.yml`, `pipeline-canary.yml` and
`pipeline.yml`. Its `release-notes.yml` does not fit a
reusable shape: it checks this repo out at `.shared` and calls
`.shared/scripts/openrouter_ai.py` directly.

## Why the dependency-bot path evolved the way it did

1. **`pull_request` + actor gate** — first attempt. Broke: GitHub gives
   `pull_request` runs a read-only `GITHUB_TOKEN` and no secrets when the
   actor is a bot — turns out **not** Dependabot-specific,
   `renovate[bot]` hit it too.
2. **`pull_request_target`** — the usual fix for #1. Its default checkout is
   the *base* branch, not the PR (fixed by explicitly checking out
   `head.sha`) — and it still can't retroactively fire for PRs opened before
   the workflow existed.
3. **Scheduled sweep** — a `schedule` run has no PR actor at all,
   so #1 doesn't exist to begin with, and it picks up a whole backlog on its
   first run. Confirmed by reading `openwrt/openwrt`'s real
   `llm-review.yml`: cron + `workflow_dispatch`, no `pull_request` trigger.
4. **The graph** (current, `v3`) — the bot's `pull_request` run was seen with the
   secrets and a token that writes (flask-test-api #313, 2026-10-12), so the limit
   of #1 no longer holds there. The sweep, a second engine, was removed: one graph
   reviews, repairs and merges every pull request.

## Per-repo setup

Each consumer repo needs, in *Settings → Secrets and variables → Actions*:

- Variable `AI_ENABLED` = `true`
- Variable `OPENROUTER_MODEL`, `OPENROUTER_ENDPOINT` (optional — defaults in
  the script), `OPENROUTER_SITE_URL`, `OPENROUTER_APP_NAME` (optional)
- Secret `OPENROUTER_API_KEY`
- Secret `AUTOFIX_PUSH_TOKEN` (the agent workflows): a fine-grained token with
  *Contents* and *Pull requests: read and write* on the repository. It pushes
  fixes and merges, so that the workflows of the branch it lands on start. Add
  *Workflows: read and write* for the merge gate to merge a change to
  `.github/workflows/` too; without it GitHub refuses that merge and such a
  change waits for a person. No agent edits a workflow file either way. The
  canary also opens its pull requests with it, so their workflows start.
- *Settings → Actions → General → Workflow permissions*: **Allow GitHub
  Actions to create and approve pull requests**, for the fix pull request
  after a direct push.
- For the canary: a small module nothing imports, its tests, and
  `.github/canary.json` describing the scenarios (edits to make, and what must
  be true afterwards). One pull request is opened per scenario, on every run
  and on every release of this repository: keep them few.

Nothing needs to be configured in this repo per consumer — it's stateless.

## This repository runs its own pipeline

Pull requests here go through the same loop they define: `agent-change.yml` and `agent-ci-failure.yml`
review and repair them, `agent-merge.yml` merges a certified pull request once `Test scripts` is green on
the same commit, and `release-tag.yml` then moves `v3`, once the canary of the consumer repository has
passed against the merged commit. The agents run at the published tag, never at the
pull request's own code, so a change that breaks a reviewer is still judged by the reviewer that works.

What stays with a person: writing a change to `.github/workflows/` (no agent edits those; since 2026-10-11
the merge gate merges them like any other change), and a breaking change that needs a new tag.

## Tests

```bash
pytest tests/ -v
```

About 411 tests, no network, no real `gh` calls — `checks_state`,
the engine's graph against real throwaway git repositories and a scripted
model, failure adjudication, the weakening guard, terminal states, the merge
gate and the main guard (with a local bare remote) are all exercised against
fakes.
`.github/workflows/test.yml` runs them on every push/PR to this repo
(installing `scripts/requirements-autofix.txt` first, for the graph tests).
