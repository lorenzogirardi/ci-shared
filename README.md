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

`v2` is not moved by hand. `release-tag.yml` moves it to a commit of `main`,
forwards only, after two things: `Test scripts` has passed on that exact
commit, and the consumer's canary has passed against it. For the second, the
commit is published as `v2-next` and the canary of the repository named in the
variable `ENGINE_GATE_REPO` is started in candidate mode: its pull requests
target a base branch named `canary/next-*`, and for those the agent workflows
check out the scripts and prompts at `v2-next`. If a scenario does not hold,
`v2` stays where it is and the run is red. Until `ENGINE_GATE_REPO` is set
(`owner/repo`; `ENGINE_GATE_WORKFLOW` names the canary workflow, default
`pipeline-canary.yml`) that second step is skipped and the tag moves after the
tests alone; a manual run of `release-tag.yml` with the `consumer` input checks
that the gate can reach a given repository. Unit tests do not see what a model
does with a real pull request; this does. A consumer whose workflow grants fewer
permissions than a reusable workflow asks for does not start at all: when a
change needs a new permission, update the callers first, then merge here.

## v2: role-based agents

`v2` adds the agents below on top of everything `v1` does; `v1` stays frozen
for repos that have not moved. All reusable workflows in `v2` check out
`ref: v2`.

| Workflow | Roles | Writes? |
|---|---|---|
| `reusable_agent-pipeline.yml` | planner -> writer -> deterministic checks -> reviewer A (correctness/design) + reviewer B (security/operability) -> dedup/validation -> bounded fix loop -> final reviewer -> documentation reviewer -> changelog | opens a PR certified at its head commit; retried once with twice the budget, then abandoned (commented, nothing pushed); never merges itself |
| `reusable_agent-change.yml` | the same engine on a change that already exists, no planner: `mode: pr` (any PR: checks, reviewers A+B, fixes pushed to the PR branch, final reviewer, docs) or `mode: push` (a push straight to the default branch: review, then a fix PR if something blocks) | fix commits on the PR branch, or a PR from `agent/push-<sha>`; never merges; ignores its own commits |
| `reusable_agent-review.yml` | reviewers A and B on any PR diff | one comment |
| `reusable_agent-merge.yml` | none (deterministic): the merge gate | squash-merges a pull request whose head commit is certified and whose required checks succeeded on that same commit; only into its own base branch; stops at the circuit breaker |
| `reusable_agent-main-guard.yml` | none (deterministic): the guard on the base branch | re-runs the failed jobs once, then reverts to the last green state and says so on the pull request the change came from (label `agent-reverted`). Opens no issue |
| `reusable_pipeline-health.yml` | none (deterministic): the loop checks itself | starts the build for a commit that has none; abandons a pull request that has no verdict and nothing left to run; goes red on a certification given while an agent could not do its job, and on an agent workflow that is failing now |
| `reusable_pipeline-canary.yml` | none of its own: sends known changes through the real loop | opens the scenarios of the consumer's `.github/canary.json` as pull requests against a throwaway copy of the base branch, checks the outcome (verdict, required checks, files changed, contents), closes them. Never merges |
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
- The loop is bounded (`max_iterations`, `max_verify_retries`,
  `writer_rounds`). When it stops without converging, the PR says so.
- `reusable_pr-review-sweep.yml` gained `skip_head_prefixes` so the sweep
  leaves `agent/` branches to the pipeline that certifies them.
- Cost per role is recorded (`.ai/agents/*.usage.json`) and shown in the PR.
- Every change ends merged, abandoned or reverted; none waits for a person.
  The rules that make that safe are code, not prompt: tests are the
  specification (a test is changed only when the change's own description says
  so, quoted word for word); a test an agent has just written is not the
  specification, so the application is never changed to satisfy it; an agent
  that cannot produce a usable answer blocks the certification; a verdict is
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

### `reusable_pr-review-sweep.yml`

The main event. A **scheduled** sweep (called from a `schedule` +
`workflow_dispatch` trigger, not `pull_request`) over open PRs: review the
ones not yet reviewed at their current head SHA, merge the clean ones whose
CI actually passed, and — opt-in — repair the broken ones.

| Input | Default | Purpose |
|---|---|---|
| `authors` | `''` (all) | Comma-separated PR author logins to sweep |
| `max_prs` | `10` | Cap per run |
| `auto_merge` | `false` | Merge PRs whose review + CI are both clean |
| `auto_merge_authors` | `''` (no restriction beyond `auto_merge`) | Comma-separated logins allowed to actually be merged. Lets `authors` be wider than this — e.g. autofix/review a human's PRs too, but only ever auto-merge a dependency bot's. |
| `required_checks` | `''` | Check-run names that must have **succeeded** before merge — see below, this is not optional in practice |
| `merge_method` | `squash` | |
| `merge_poll_seconds` | `90` | After a fresh push, wait up to this long in this job for required checks to settle before deferring the merge to the next sweep. `0` disables polling. |
| `merge_poll_interval` | `15` | Seconds between polls while waiting on `merge_poll_seconds` |
| `triage_on_failure` | `false` | On red CI, explain the failure instead of reviewing the diff |
| `autofix` | `false` | On red CI, push a verified fix instead of only explaining. Implies `triage_on_failure`. No file-type restriction beyond `.github/workflows/**`; a reply may also list a directory, find a file by name, grep file contents, or read a real file (repo, installed package, or stdlib) before proposing an edit — see architecture.md. |
| `python_version` | `3.12` | Interpreter `verify_command` runs under — **must match the real CI gate's version** |
| `verify_command` | `''` | Shell command proving an autofix edit works, run before pushing anything |
| `verify_timeout_seconds` | `180` | |
| `max_autofix_attempts` | `3` | Propose→verify rounds tried locally before giving up on one PR — a `{"read": ...}` reply costs a round too, so code-level migrations need more than a manifest revert does |

Secrets: `openrouter_api_key` (required), `autofix_push_token` (optional — see architecture.md; without it, autofix's push authenticates as `GITHUB_TOKEN` and GitHub silently suppresses the resulting CI run).

**The merge gate is CI, not the review verdict.** `required_checks` names
checks that must show `conclusion: success` — not merely "didn't fail".
Without it, the fallback only requires *some* check to have succeeded, which
is close to vacuous: a PR whose only check is `ai-review.yml` reporting
`skipped` (it skips bot authors) used to read as green and get merged with
nothing tested. Two dependency bumps that a clean AI review waved through
broke `main` in one session — a resolver conflict and a runtime 500 — both
things a command proves in seconds and a diff review can only guess at.

**Autofix is agentic, not one-shot.** On a red PR it proposes an edit
(strict-JSON, `{file, find, replace}`, validated in code: manifest files
only, unique anchor, ≤5 edits, never a fork), applies it, and runs
`verify_command` **in this job** before pushing anything. A pass commits
(with an explicit git identity — a fresh checkout has none) and pushes
immediately; a failure reverts the edit, feeds the real verification output
back into the next prompt, and retries. Nothing reaches the PR branch until
one attempt verifies or every attempt is exhausted. The commit and PR
comment both say plainly that a machine wrote it, unreviewed, and that the
real CI on the pushed commit — not this loop — decides whether it merges.
The repair loop is the same engine as every other change (`scripts/agent_pipeline.py`),
started from the failing CI logs: writer, deterministic checks in a secret-free
process, failure adjudication (is the code or the test wrong), two independent
reviewers; retried once with twice the budget, then the PR is abandoned. Before
the model reads anything, the checks run once on the PR as the bot left it, so
the NEW dependency version is installed and its source can be read. The push
token is taken out of `.git/config` while the PR's code runs and put back only
for the push. There used to be a separate `autofix_core.py` graph and a
`reusable_main-autofix.yml` for a red base branch; both are gone: a red base
branch is handled by `reusable_agent-main-guard.yml` (revert, then redo).

### `reusable_ci-analysis.yml`

Post-pipeline informative report: downloads `ai-context-*` artifacts from
earlier jobs (lint/test/trivy/checkov/k8s-probe), bundles them with the app
source, asks the model for a security/quality report, posts it to the job
summary and as an artifact. Never gates the pipeline —
`continue-on-error: true` is set inside the reusable workflow's own job (a
job that *calls* a reusable workflow can't set that itself).

Secret: `openrouter_api_key` (required).

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

Scheduled sweep with merge + self-repair, for a dependency bot only:

```yaml
on:
  schedule: [{cron: '0 4,16 * * *'}]
  workflow_dispatch:

permissions: {}

jobs:
  sweep:
    if: vars.AI_ENABLED == 'true'
    permissions:
      contents: write
      pull-requests: write
    uses: lorenzogirardi/ci-shared/.github/workflows/reusable_pr-review-sweep.yml@v1
    with:
      authors: 'renovate[bot]'
      auto_merge: true
      required_checks: 'checks,workflows'   # your pr-checks.yml job names
      triage_on_failure: true
      autofix: true
      python_version: "3.14"                # must match your real CI gate
      verify_command: |
        set -euo pipefail
        pip install --dry-run -r requirements.txt
        pip install -q -r requirements.txt
        uvicorn app.main:app --host 127.0.0.1 --port 8000 &
        UVICORN_PID=$!
        trap 'kill $UVICORN_PID 2>/dev/null || true' EXIT
        for i in $(seq 1 30); do
          curl -sf -o /dev/null http://127.0.0.1:8000/api/mgmt/ready && exit 0
          sleep 1
        done
        exit 1
    secrets:
      openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

See `flask-test-api/.github/workflows/ai-review.yml`, `ai-review-sweep.yml`,
and `pipeline.yml`'s `ai-analysis` job for the real wrappers.
`release-notes.yml` and `issue-triage.yml` don't fit either reusable shape —
they check this repo out at `.shared` and call
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
3. **Scheduled sweep** (current) — a `schedule` run has no PR actor at all,
   so #1 doesn't exist to begin with, and it picks up a whole backlog on its
   first run. Confirmed by reading `openwrt/openwrt`'s real
   `llm-review.yml`: cron + `workflow_dispatch`, no `pull_request` trigger.

## Per-repo setup

Each consumer repo needs, in *Settings → Secrets and variables → Actions*:

- Variable `AI_ENABLED` = `true`
- Variable `OPENROUTER_MODEL`, `OPENROUTER_ENDPOINT` (optional — defaults in
  the script), `OPENROUTER_SITE_URL`, `OPENROUTER_APP_NAME` (optional)
- Secret `OPENROUTER_API_KEY`
- Secret `AUTOFIX_PUSH_TOKEN` (the agent workflows): a fine-grained token with
  *Contents: read and write* on the repository, never the `workflow` scope. It
  pushes fixes and merges, so that the workflows of the branch it lands on
  start. It does not need to open pull requests: those are opened with the
  job's own token.
- *Settings → Actions → General → Workflow permissions*: **Allow GitHub
  Actions to create and approve pull requests**, for the canary and for the
  fix pull request after a direct push.
- For the canary: a small module nothing imports, its tests, and
  `.github/canary.json` describing the scenarios (edits to make, and what must
  be true afterwards).

Nothing needs to be configured in this repo per consumer — it's stateless.

## This repository runs its own pipeline

Pull requests here go through the same loop they define: `agent-change.yml` and `agent-ci-failure.yml`
review and repair them, `agent-merge.yml` merges a certified pull request once `Test scripts` is green on
the same commit, and `release-tag.yml` then moves `v2`. The agents run at the published tag, never at the
pull request's own code, so a change that breaks a reviewer is still judged by the reviewer that works.

Two things stay with a person: a pull request that touches `.github/workflows/` (no agent may edit or merge
those), and a breaking change that needs a new tag.

## Tests

```bash
pytest tests/ -v
```

430+ tests, no network, no real `gh` calls — `checks_state`, verdict parsing,
the engine's graph against real throwaway git repositories and a scripted
model, failure adjudication, the weakening guard, the merge gate and the main
guard (with a local bare remote), terminal states, and `auto_merge_authors`
gating are all exercised against fakes.
`.github/workflows/test.yml` runs them on every push/PR to this repo
(installing `scripts/requirements-autofix.txt` first, for the graph tests).
