# Status — AI-native pipeline

Written 2026-10-11 13:40 UTC as a handoff: the work continues from another machine with no memory of
this session. Read `CLAUDE.md` first (intent, rules, tools, the 38 team actions), then this file.

Engine: `lorenzogirardi/ci-shared` (tag `v2`, moved only by `release-tag.yml`).
Consumer and proving ground: `lorenzogirardi/flask-test-api`.

## Target

```
team actions (PR, direct push, dependency bot)
        │
        ▼
deterministic tools ── does the app work? (gate)      tests, build, deploy
        │           └─ awareness (never blocks)       Trivy, Checkov, SBOM, TruffleHog
        ▼
one LangGraph graph: developer → reviewers by role
        developer 2 · QA tester · SRE · architect · security (reports tools) · doc writer
        ▼
final decision, no person (owner's risk level 1-10, default 1 = never asks)
        ▼
merged · abandoned · reverted        + warnings and every role's verdict visible on the run
```

## Where things are right now

| Thing | State |
|---|---|
| `ci-shared` `main` | `33014fb` (#52) |
| `ci-shared` `v2` (what every consumer runs) | `c80ae83` (#51): **one commit behind `main`** |
| Why behind | the release canary of #52 failed: scenario `intent` (flask-test-api#273) ended abandoned |
| ci-shared #53 `fix/architect-informs` | open, agents reviewing. The fix for that failure |
| ci-shared #50 `feat/risk-level` | open, agent check red (the 49-minute run, cancelled by hand). Step 4 of the plan |
| flask-test-api | `main` green, no open pull request |

## Next actions, in order

1. **Get #53 merged and `v2` released.** It touches no workflow file, so the agents merge it when
   they certify it. The release that follows carries #52 and #53 together. Check:
   `gh pr view 53 --repo lorenzogirardi/ci-shared`, then that `v2` equals `main`
   (`git fetch --tags -f origin && git rev-parse origin/main v2^{commit}`). If the canary fails again,
   read which scenario and why (see "How to look at a run") before changing anything.
2. **Resume #50 (risk level).** Rebase `feat/risk-level` on `main` (conflicts expected in `CLAUDE.md`,
   `status.md`, `docs/architecture.md`: keep `main`'s text and re-add the step 4 lines). Rule 1 of
   `CLAUDE.md` already names the risk level as its one exception, which was the architect's objection.
   #50 touches `reusable_agent-change.yml`, so it is merged by hand once certified and green.
3. **Step 5: the dependency bot through the same graph.** Not started. The largest step: the sweep
   (`scripts/pr_review_sweep.py`) keeps only "pick the bot's pull requests, ask for a rebase when
   behind, leave the merge of workflow pull requests to the bot"; review, repair and merge come from
   the graph and the existing merge gate; its own review, triage, autofix and merge code is deleted.
4. **Step 6: remove `ai-analysis`** (`pipeline.yml` in flask, `reusable_ci-analysis.yml` here): the
   graph now reads the same artifacts.
5. Update the state column of the 38 actions in `CLAUDE.md` in each change that moves one.

## The plan and what is done

Approved by the owner on 2026-10-11. One pull request per step and repository, released by the gate
(tests, then the consumer's canary against `v2-next`), proven in a real run before the next starts.

| Step | What | Where | Seen in a real run |
|---|---|---|---|
| 1 | Trivy, Checkov, TruffleHog and the deploy state on every pull request, as warnings and `ai-context-*` artifacts; TruffleHog on `main` | flask #262 | yes |
| 2 | The engine downloads those artifacts at the first review; security reads them and reports under "Awareness (not blocking)" | ci-shared #48 | yes: the canary's pull requests carry the section |
| 3 | `reviewer-sre` and `reviewer-architect` added to the reviewers of the graph | ci-shared #49 | yes: four roles, three canary pull requests certified, $0.011-0.019 each |
| 4 | Risk score on every pull request; input `risk_level` 1-10 (default 1, never asks); above 1 a certified change waits for the label `risk-confirmed` | ci-shared #50 | **open** |
| 5 | The dependency bot through the same graph | — | not started |
| 6 | Remove `ai-analysis` and what else was replaced | — | not started |

Outside the plan, each for a defect seen in a real run:

| What | Where | State |
|---|---|---|
| Health check starts on push and after `PR Checks` | flask #244 | released, seen |
| The sweep reviews the bot's workflow pull requests and writes the status `agent-review` | ci-shared #44, flask #246 | released; **not seen**: needs the bot's next workflow pull request |
| Python version written only in the `Dockerfile` | ci-shared #45, flask #257 | released, seen |
| Trivy and Checkov findings as warning and summary on `main` | flask #258 | released, seen (Checkov) |
| A model call is stopped after 5 minutes, a run after 15, the job after 25; one log line per model call | ci-shared #51 | released, seen (the lines) |
| The developer agent is shown the code around each finding; the refusal of an unusable reply says what to send | ci-shared #52 | on `main`, **not released** |
| The architect only informs | ci-shared #53 | open |

## What went wrong today, and what it taught

- **A 49-minute agent run** (ci-shared #50, run 38138539119). One model call, the correctness
  reviewer, held the review for more than twenty minutes, twice, with two lines in the log. The
  request timeout is per socket read and the empty-reply retries multiply it. Fixed by #51. Actions
  minutes are limited: the owner will not accept a run like this again.
- **The developer agent used eight rounds without proposing a change** (same run). Six replies were
  refused for their format (several requests in one reply, commentary, tool-call markup of another
  system); the one file it read is shown only from the beginning and the finding was at line 1355.
  Fixed by #52.
- **The architect sent changes back over rules written in the project's documents**, and they ended
  abandoned: #50 (rule 1 of `CLAUDE.md`, a fair point, since fixed in the text) and the canary's own
  `intent` scenario (the rule about the canary module in flask's `CLAUDE.md`). The plan's stop
  condition applied: #53 makes the architect advisory. That the canary's blocking finding came from
  the architect is inferred (it is the only reviewer shown the project's documents), not read from
  the trace.
- **Slow cycle.** A change to the engine costs about 10 minutes of release (tests 1.7, canary 8).
  The models are about 35 seconds of that; the rest is `PR Checks` (2.5 minutes, image build and
  deploy) which runs twice, on the pull request and on the agents' commit. Several small engine pull
  requests in a row queue their releases: group engine changes into fewer pull requests.
- **#51 was merged without the agents' certification**: its review was running on the engine without
  the time bound, the one that had just hung. Tests were green. The only one today.

## How to work on this (the other machine has no memory)

The owner's standing instructions, in their words where it matters:

- **"NON fare over engineering"**, repeated several times, also for fixes inside the approved plan. A
  change needs a defect seen in a real run, or a missing role of the intent. The smallest change to
  an existing piece; no new option, fallback or helper "while I am here". List other ideas, do not
  build them.
- **Do the work, do not ask.** Merge, dispatch and operate yourself. Ask only what is truly the
  owner's decision, in plain words, and say whether it is "a change to the pipeline itself" or "a
  person's step in the flow of the code" (only the second breaks the goal).
- **Answer when asked "dove siamo?"**: a short status first, every time, even mid-task. Long silent
  stretches are not accepted.
- **Say what was seen in a real run and what was not.** Never describe behaviour as working because
  the code exists.
- The owner writes in Italian; answers in Italian, short, concrete.
- Never use the word "blocks" for an agent's finding: a role either hands work to the developer agent
  or leaves a note. Awareness tools only ever leave a note.

Practical facts that cost time to learn:

- A change that touches `.github/workflows/` cannot be merged by the agents (no `workflow` scope):
  wait for their certification and the checks, then `gh pr merge <n> --squash --delete-branch`, as its
  own short command. Everything else they merge themselves.
- A reusable workflow that needs a new permission or input: change the consumer's caller first, then
  let `v2` move. Never move `v2` by hand.
- The agents reviewing a pull request of this repository run at the released `v2`, not at the pull
  request's own code. New engine behaviour is first seen in the release canary.
- In flask-test-api never `git add -A`: the untracked `.claude/` holds the owner's local files.
- Tests here: `uv run --no-project --python 3.12 --with pytest --with-requirements scripts/requirements-autofix.txt pytest tests -q`.
  One test fails only locally (`test_resolves_an_installed_package_file`); it passes in CI.
- Scheduled workflows on GitHub start hours late; nothing may depend on the clock alone.
- Secrets are typed by the owner in their own terminal, never pasted in chat.

How to look at a run:

- Verdict and findings: the comment marked `<!-- agent-pr -->` on the pull request.
- Per-call timing: `gh run view <id> --log | grep "model call"`.
- Every prompt and reply of a run: the artifact `agent-change-pr-<run id>` (`gh run download <id>`),
  files `NNN-<role>.user.txt` and `.reply.txt`.
- Canary result: the log of `Pipeline Canary` in flask-test-api, lines `### <scenario>: passed|FAILED`.

## Not verified in a real run

- The dependency bot waiting for the status `agent-review` before merging a workflow pull request.
- A base-image update (`Dockerfile`, Python 3.15 when its image is published) taken to merge by the
  agents alone.
- Trivy or TruffleHog reporting a finding (the image has no critical vulnerability, no secret was committed).
- The fix pull request after a direct push to `main`.
- A run actually ending at its 15-minute budget.
- The risk level asking for confirmation (unit tests only; no consumer sets a level above 1).

## Left for the owner

- Issues (actions 30-33 in `CLAUDE.md`): out of this plan by choice on 2026-10-11; `planner.md` and
  the issue flow of the engine are kept for it.
- The blog post `skill-blog/content/posts/autopsy-of-an-agentic-loop.md` is still `draft: true` and
  describes the system before today's roles.
