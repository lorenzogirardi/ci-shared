# Status — AI-native pipeline

Written 2026-10-11. Where the work stands and what is left, against the intent in `CLAUDE.md`.
Engine: `lorenzogirardi/ci-shared` (`v2`). Consumer: `lorenzogirardi/flask-test-api`.

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
final decision, no person (optional risk level 1-10, default 1)
        ▼
merged · abandoned · reverted        + warnings and every role's verdict visible on the run
```

## Done today (2026-10-11), all merged and released

| What | Where | Seen in a real run |
|---|---|---|
| Health check starts on push and after `PR Checks`, not only on the clock | flask #244 | yes |
| The sweep reviews the bot's workflow pull requests and writes the status `agent-review` | ci-shared #44, flask #246 | no: needs the bot's next workflow pull request |
| Python version written only in the `Dockerfile`; every workflow reads it there | ci-shared #45, flask #257 | yes (CI back on 3.14.8, same as the image) |
| `CLAUDE.md`: intent, rules, tools, roles, 38 team actions with their state | ci-shared #46 | merged by the agents |
| Rule: security and quality tools give awareness and never gate | ci-shared #47 | merged by the agents |
| Trivy and Checkov findings as a warning and a summary section on the run | flask #258 | Checkov yes (9 findings, job green); Trivy had nothing to report |

## Plan steps done

| Step | What | Where | Seen in a real run |
|---|---|---|---|
| 1 | Trivy, Checkov, TruffleHog and the deploy state on every pull request, as warnings and `ai-context-*` artifacts; TruffleHog on `main` | flask #262 | yes: four artifacts, Checkov warning, all required checks green |
| 2 | The engine downloads those artifacts at the first review; `reviewer-security` reads them and reports under "Awareness (not blocking)" | ci-shared #48 | yes: the canary's pull requests carry the section, quoting Checkov, Trivy and the deploy |
| 3 | `reviewer-sre` and `reviewer-architect` added to the reviewers of the graph | ci-shared (this change) | not yet |

## Notes from reading the engine

- Findings from reading `scripts/agent_pipeline.py` before step 2:
  - the reviewers of the graph are `REVIEWERS = ("reviewer-correctness", "reviewer-security")`, run in
    parallel by `review_diff`; they receive the plan and the diff, and **no tool output at all**
    (`Runtime.guidance` is never set on the pull-request path);
  - the dependency sweep is the only place that passes check results to the reviewers;
  - the consumer's `main` build already uploads `ai-context-*` artifacts (lint, tests, Trivy, Checkov,
    deploy probe); `PR Checks` uploads none;
  - `Agent Change` starts together with `PR Checks`, so evidence from the checks is not there when
    the run starts.

## To do

Each item is one pull request, proven by the canary before the next one starts.

1. **Awareness tools on the pull request** (consumer). `PR Checks` runs Trivy on the image it already
   builds, Checkov on the chart and TruffleHog on the commits; none can fail the job; each writes a
   warning, a summary section and an `ai-context-*` artifact. TruffleHog also on `main`.
2. **Evidence reaches the graph** (engine). Before the review node, the engine downloads the
   `ai-context-*` artifacts of the commit (waits a bounded time, goes on without them) and gives them
   to the roles that need them. What a role says about them is reported under "Awareness (not
   blocking)" in the pull request comment; it never holds the change.
3. **SRE and architect as reviewers** (engine). Two prompts, `reviewer-sre` (build, deploy, health,
   with the evidence) and `reviewer-architect` (fit with the design, with the context files), added to
   the reviewers of the graph. `planner.md` stays: it belongs to the issue flow, which is out for now.
4. **Risk level** (engine, optional for the consumer). Input `risk_level` 1-10, default 1.
   1 = never asks; 10 = always asks for confirmation; in between, asks when the change's risk score
   (computed from facts: files touched, size, areas, tool findings) is at least `11 - risk_level`.
   Asking = certified but not merged, label and comment, merged when a person confirms.
5. **The dependency bot through the same graph** (engine). The sweep only picks the pull request and
   starts the graph; its own review, fix and merge logic is removed.
6. **`ci-analysis` removed** (consumer and engine), once the graph reads the same artifacts.
7. Update `CLAUDE.md` (state column of the 38 actions) and `docs/architecture.md` in each change.

## Open questions for the owner

- Issues (actions 30-33 in `CLAUDE.md`): removed earlier by choice, listed again as an example. In or out?
- Risk level: how a person confirms (a label on the pull request, or an approving review).
- Should an architect or SRE finding on a changed line be able to block, like the other reviewers, or
  only inform?

## Not verified in a real run

- The dependency bot waiting for `agent-review` before merging.
- A base-image update (`Dockerfile`) taken to merge by the agents alone.
- Trivy reporting a finding on the run (the image currently has no critical vulnerability).
- The fix pull request after a direct push to `main`.
