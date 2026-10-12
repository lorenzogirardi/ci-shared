# Status — AI-native pipeline

Written 2026-10-11 as a handoff and brought up to date on 2026-10-12 21:00 UTC: the work continues from another machine with no memory of
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
| `ci-shared` `main` and `v2` (what every consumer runs) | equal after every release; check with `git fetch --tags -f origin && git rev-parse origin/main v2^{commit}` |
| Seen on the consumer on 2026-10-11/12 | a real change to the app through the whole graph, merged with no person in 6 minutes (flask #302); two changes to workflow files merged by the agents (flask #306, #307); the canary with one pull request (flask #305) |
| The dependency bot | since flask #307 the graph reviews its pull requests too, next to the sweep. Seen on flask #313: reviewed and merged by the graph in 3 minutes, with two comments because both engines worked on it |
| **Branch `feat/architect-first`, local, not pushed** | the new strategy the owner asked for on 2026-10-12: every change enters at the architect and is closed by it. See the section below. Released `v2` does not have it |
| The model key | its monthly limit was reached on 2026-10-11 and every pull request was abandoned until the owner raised it; the comment said "the test steward could not produce a usable answer", the cause was only in the log. A commit abandoned for that is not tried again without a new push |
| ci-shared #50 `feat/risk-level` | **closed by accident** on 2026-10-11 (the commit message of #58 said "resolved (#50": GitHub took it as a closing word). The branch is there. Not to be merged as it is: once the owner puts `risk-confirmed` on a pull request, every later commit merges without asking again. Step 4 of the plan |
| flask-test-api | `main` green, no open pull request |

## The architect first (branch `feat/architect-first`, 2026-10-12)

The owner's strategy, replacing "the reviewers each read the diff": the architect reads the code and the
facts and writes a brief; developer 1 (always the agent) does it; developer 2 pairs, three rounds at
most, then the architect decides; QA, SRE and security in parallel, documents; the architect closes.
`docs/architecture.md` describes it; the approved plan is in the owner's session, not in the repository.

What is on the branch, all of it with unit tests (504 passing):

- the graph above, with a second cycle from the architect when the work reaches a dead end;
- a record of every run in `agents-run/` (journal and the graph's state after every node), node retries
  by LangGraph, SRE and security as two parallel nodes;
- nine tools the architect chooses among, next to list/find/grep/read: `docs`, `usages`, `imports`,
  `signature`, `history`, `tests`, `api`, `evidence`, `runs`. The owner's words: it must have the tools,
  and it decides whether to use them;
- QA thinks before it writes; a test it cannot write no longer ends a change that works;
- what the brief asked for and nobody did goes back to the developer once, from the close.

Seen, locally, on the replay of flask #118 (Renovate, `mcp` 1 to 2, checks red), the case the released
graph abandoned in 41 seconds:

| Run | What was different | Outcome |
|---|---|---|
| 1 to 3 | architect without tools | abandoned |
| 4 | same | converged in the second cycle, 430 s |
| 5 | with the tools; QA with no thinking | brief right at once, checks passed at the first change; abandoned, 413 s, because QA wrote a test that did not run, three times |
| 6 | QA thinks | converged in one cycle, 162 s |
| 7 | QA cannot end a change; `docs` reads several sections | converged in one cycle, 190 s; QA changed a test and it passed |

In runs 5 to 7 the architect opened the library's migration guide by itself (`docs`) and its brief
named the test file the earlier briefs had missed.

Not seen: the integration test of that change executed (no PostgreSQL and Redis locally); the close
sending work back; QA failing and the change going on; a feature and a bug through the new graph
(`~/ci-exp/run-cases.sh` replays flask #302 and the first version of ci-shared #53; it needs the model
key in the terminal that starts it, the session may not type it); any of this in the pipeline.

A warning the runs raised and nothing acts on: `mcp` 2 refuses requests whose Host is not local, so
`/api/mcp` may stop answering behind an ingress. Only the deploy check of the real pipeline can say.

## Next actions, in order

1. **Finish and publish `feat/architect-first`**: the two local replays above, then ONE pull request on
   ci-shared (every release opens a canary pull request on the consumer). The agents that review it run
   the released engine; the canary judges the new one. The consumer's `docs/13-agent-pipeline.md` after it.
2. **Remove the sweep** once a dependency update with red checks is seen repaired by the released graph:
   flask `renovate.json` (`rebaseWhen`, drop the workflow `automerge` rule), `agent-ci-failure.yml`
   (drop the `renovate/` exclusion), delete `ai-review-sweep.yml`; here the Renovate exclusion in
   `cmd_merge_gate`, `reusable_pr-review-sweep.yml`, the review part of `pr_review_sweep.py`,
   `planner.md`, `reviewer-architect.md`, `invoke_with_retry`.
3. **#50 (risk level).** Closed by accident, the branch is there. Fix the confirmation first: it must
   be bound to the commit that asked for it.
4. **The consumer's release notes** (`release-notes.yml`): the one model call left outside the graph.
   The owner has not said whether it stays.
5. Readable messages still to do: the canary's label and closing comment; awareness limited to what is
   new against the base.
6. Later, by the owner's choice: a subgraph for the developer pair; resuming from the checkpoints when
   `agents-run` is a bucket; the issue entry; interrupts for the risk level.
7. Update the state column of the 38 actions in `CLAUDE.md` in each change that moves one.

## The plan and what is done

Approved by the owner on 2026-10-11. One pull request per step and repository, released by the gate
(tests, then the consumer's canary against `v2-next`), proven in a real run before the next starts.

| Step | What | Where | Seen in a real run |
|---|---|---|---|
| 1 | Trivy, Checkov, TruffleHog and the deploy state on every pull request, as warnings and `ai-context-*` artifacts; TruffleHog on `main` | flask #262 | yes |
| 2 | The engine downloads those artifacts at the first review; security reads them and reports under "Awareness (not blocking)" | ci-shared #48 | yes: the canary's pull requests carry the section |
| 3 | `reviewer-sre` and `reviewer-architect` added to the reviewers of the graph | ci-shared #49 | yes: four roles, three canary pull requests certified, $0.011-0.019 each |
| 4 | Risk score on every pull request; input `risk_level` 1-10 (default 1, never asks); above 1 a certified change waits for the label `risk-confirmed` | ci-shared #50 | **closed, not merged**: the confirmation is not bound to the commit |
| 5 | The dependency bot through the same graph | flask #307 (the graph reviews them too) | first step, **not seen**: needs the bot's next pull request |
| 6 | Remove `ai-analysis` and what else was replaced | flask #306, ci-shared #63 | yes |

Outside the plan, each for a defect seen in a real run:

| What | Where | State |
|---|---|---|
| Health check starts on push and after `PR Checks` | flask #244 | released, seen |
| The sweep reviews the bot's workflow pull requests and writes the status `agent-review` | ci-shared #44, flask #246 | released; **not seen**: needs the bot's next workflow pull request |
| Python version written only in the `Dockerfile` | ci-shared #45, flask #257 | released, seen |
| Trivy and Checkov findings as warning and summary on `main` | flask #258 | released, seen (Checkov) |
| A model call is stopped after 5 minutes, a run after 15, the job after 25; one log line per model call | ci-shared #51 | released, seen (the lines) |
| The developer agent is shown the code around each finding; the refusal of an unusable reply says what to send | ci-shared #52 | released, seen: the canary passed with it |
| The architect only informs | ci-shared #53 | replaced by #60: every role but the second developer informs |
| A stopped call says what the model script reported | ci-shared #55 | released, seen (#57: "empty reply, retrying") |
| The correctness reviewer has a narrower task and a cap of 5 findings | ci-shared #56 | released; it did not shorten the wait |
| One retry of an empty reply, with the same budget | ci-shared #57 | released |
| A reviewer asks for a fix once and never ends a change whose checks pass | ci-shared #58 | released; "never ends" seen only in unit tests |
| One loop: only the second developer sends back and rereads the fix; no final reviewer; no second attempt on a pull request | ci-shared #60 | released, the canary passed |
| Every role but the writer answers with no thinking | ci-shared #59, #60, #62 | released, seen: the six reviews of #61 took 9 seconds of model time in all; a real change on flask (#302) merged in 6 minutes |
| The merge gate merges a change to a workflow file | ci-shared #61 | released, seen (flask #306, #307, #308; ci-shared #63) |
| The canary opens one pull request | flask #303 | seen (flask #305). The `intent` scenario left the canary |
| The `image` job does not rebuild for a commit that only changes unit tests | flask #293 | merged; **not seen**: on #302 the agents' last commit was documentation, so it rebuilt |
| The SBOM of the pull request's image is evidence | flask #308 | the artifact is produced (210 packages); **not seen** in a role's awareness yet |

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

## The slow reviewer, measured on 2026-10-11

`reviewer-correctness` was the wait in every slow run (212 to 300 seconds, often with no answer). About
340 calls to the model outside the pipeline, on the real inputs of #50, #53 and #57 (the experiment is in
`~/ci-exp` on the owner's machine, not in the repository):

- With thinking on, no wording of the prompt gave both speed and sight: seven versions, the owner's and
  the caveman skill included, either gave no answer on a large diff or stopped making the remark on #53 (see the correction below).
  Giving it the surrounding code, before or after the change, did not shorten it.
- With thinking off it answered in 1 to 16 seconds every time and remarked on the first version of #53
  every time, but marked `high` what was not: 9 false `high` in 10 runs on two correct changes.
  **Corrected on 2026-10-12**: what #53 held was not a defect. It tested the first finding's reviewer
  where the fix tests the reviewer being read; every list holds one reviewer's findings, so the two behave
  the same. "Saw the real defect" in these measures means "made that remark", nothing more.
- Deciding `severity` last in the answer brought that to 3 in 10, with no finding contradicting itself.

Hence: that reviewer answers with no thinking and decides severity last; and a reviewer asks for a fix
once and never ends a change whose checks pass, so a wrong `high` costs one round, not the change.

Then the wait moved to the next role that thinks (security 132 seconds, the test steward over 180), so the
owner had every role answer with no thinking, "for now": measured for the correctness and security
reviewers and the test steward (1 to 10 seconds, the missing tests still written), not for the writer, the
SRE, the architect or the documentation reviewer. And the graph lost what repeated itself: only the second
developer sends a change back and rereads the fix; no final reviewer; no second attempt on a pull request.

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

- The agents merge every certified, green pull request, those that touch `.github/workflows/`
  included (since ci-shared #61; the token has the `workflow` permission). No agent edits those files.
- A session of Claude Code may be refused, by its own permission rules, the merge of a pull request and
  a change that removes a protection of the pipeline: the owner then runs the command with `!`.
- A commit the agents abandoned is not reviewed again by a re-run: push a commit, an empty one will do.
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
- A change that goes on to be certified with a finding of the second developer left unresolved.
- A change of behaviour made on purpose, where a test must be updated, since it left the canary.
- The security role reporting the SBOM.

## Left for the owner

- **Rotate the model key.** It was pasted in a chat session on 2026-10-11; `OPENROUTER_API_KEY` is to be
  replaced in both repositories.

- Issues (actions 30-33 in `CLAUDE.md`): out of this plan by choice on 2026-10-11; `planner.md` and
  the issue flow of the engine are kept for it.
- The blog post `skill-blog/content/posts/autopsy-of-an-agentic-loop.md` is still `draft: true` and
  describes the system before today's roles.
