# ci-shared

Reusable GitHub Actions workflows shared across lorenzogirardi's repos, so the
AI-review/analysis logic and its scripts live in one place instead of being
copy-pasted per repo (it already was, byte-for-byte, between `flask-test-api`
and `cloudflare-free-exporter`).

Uses the same OpenAI-compatible client (`scripts/openrouter_ai.py`, default
endpoint OpenCode Zen, works with any `OPENROUTER_ENDPOINT`/`OPENROUTER_MODEL`
including OpenRouter) that consumer repos already had. No Claude API, no
Claude Code routines — plain HTTP call from a stdlib-only Python script.

## Versioning

Consumers pin a tag (`@v1`), never `@main`, so a change here can't silently
break every consumer at once. Each reusable workflow checks out `scripts/`
with a **literal** `ref: v1` in its own "Checkout shared scripts" step —
not resolved dynamically (`github.workflow_ref` inside a called reusable
workflow reflects the *caller's* ref, e.g. a PR merge ref in the consumer
repo, not the tag pinned in this repo's own `uses:` line, so that can't be
used to self-reference). Because the checkout step ships as part of the
tag's own content, bumping the tag and bumping that literal ref happen in
the same commit by construction — they can't drift apart.

A behavior change moves the `v1` tag forward (`git tag -f v1 && git push -f origin v1`) once verified against the real consumers; a breaking change instead gets a new `v2` tag (and its own `ref: v2` literal in the checkout steps) so existing `@v1` consumers are unaffected until they bump on purpose.

## Workflows

### `reusable_pr-diff-review.yml`

Diffs `base...head` of a PR, sends it to the model, posts/updates a single PR
comment. Called from a `pull_request`-triggered workflow for normal PR review,
or from a `pull_request_target`-triggered workflow for cases needing secrets
that `pull_request` won't provide (see Dependabot below).

Inputs: `system_prompt_extra` (project-specific review focus, appended to the
generic prompt — meant for short, caller-specific overrides like the
Dependabot prompt below), `comment_marker` / `comment_heading` (so two callers
in the same repo — e.g. normal review + Dependabot review — don't fight over
the same comment), `max_chars`, `timeout_seconds`, `openrouter_model` /
`openrouter_endpoint` / `openrouter_site_url` / `openrouter_app_name` (forward
your repo vars here — `workflow_call` does not inherit `vars.*` automatically).

If the consumer repo has a `.github/ai-review-rules.md` file, it's read and
appended to the prompt automatically (no input needed) — this is where a
project keeps its own durable review guidance (deprecated patterns to flag,
domain-specific gotchas), living next to the code it concerns instead of in
this repo.

Secret: `openrouter_api_key` (optional — omit or pass empty to no-op the AI
step while still posting a "did not run" comment; used by the caller to blank
the key on fork PRs).

### `reusable_ci-analysis.yml`

Post-pipeline informative report: downloads `ai-context-*` artifacts uploaded
by earlier jobs in the same run (lint/test/trivy/checkov/k8s-probe output),
bundles them with the app source, asks the model for a security/quality
report, posts it to the job summary and as an artifact. Never gates the
pipeline — `continue-on-error: true` is set inside the reusable workflow's job
(a job that calls a reusable workflow can't set that itself).

Inputs: `source_glob` (default `app`), size/token/timeout caps, same
`openrouter_*` passthrough as above.

Secret: `openrouter_api_key` (required).

## Why Dependabot needs its own caller workflow

GitHub gives `pull_request`-triggered runs a **read-only `GITHUB_TOKEN` and no
repo secrets** when the PR was opened by Dependabot — even though it's not a
fork. A plain `pull_request`-triggered `ai-review.yml` silently no-ops on
every Dependabot PR (`OPENROUTER_API_KEY` is empty, comment posting has no
write token). The fix: a **separate** workflow triggered by
`pull_request_target` (which always runs with the base repo's token/secrets,
regardless of actor), gated to `github.actor == 'dependabot[bot]'`, calling
the same `reusable_pr-diff-review.yml` with a dependency-bump-focused
`system_prompt_extra`. It only ever reads/diffs the PR branch — never checks
out or executes anything from it beyond `git diff` — so it doesn't reopen the
classic `pull_request_target` code-execution hole.

## Consumer wrapper example

```yaml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    if: vars.AI_ENABLED == 'true'
    uses: lorenzogirardi/ci-shared/.github/workflows/reusable_pr-diff-review.yml@v1
    with:
      openrouter_model: ${{ vars.OPENROUTER_MODEL }}
      openrouter_endpoint: ${{ vars.OPENROUTER_ENDPOINT }}
      openrouter_site_url: ${{ vars.OPENROUTER_SITE_URL }}
      openrouter_app_name: ${{ vars.OPENROUTER_APP_NAME }}
    secrets:
      openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

See `flask-test-api/.github/workflows/ai-review.yml`,
`flask-test-api/.github/workflows/dependabot-ai-review.yml` and
`flask-test-api/.github/workflows/pipeline.yml` (`ai-analysis` job) for the
real wrappers. `flask-test-api/.github/workflows/release-notes.yml` and
`issue-triage.yml` call `scripts/openrouter_ai.py` directly (not through a
reusable workflow, since they don't fit either shape above) — they check out
this repo at `.shared` and invoke `.shared/scripts/openrouter_ai.py`.

## Per-repo setup

Each consumer repo needs, in *Settings → Secrets and variables → Actions*:

- Variable `AI_ENABLED` = `true`
- Variable `OPENROUTER_MODEL`, `OPENROUTER_ENDPOINT` (optional — defaults in
  the script), `OPENROUTER_SITE_URL`, `OPENROUTER_APP_NAME` (optional)
- Secret `OPENROUTER_API_KEY`

Nothing needs to be configured in this repo per consumer — it's stateless.
