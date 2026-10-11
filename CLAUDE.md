# CLAUDE.md — the AI-native pipeline

This repository is the engine of an AI-native CI/CD pipeline. `lorenzogirardi/flask-test-api` is its
first consumer and the place where it is proven. Read this before changing anything here or in a
consumer's `.github/`.

## Intent

The pipeline does the work of a team on every change, with no person in it:

| Role | What it does for the change |
|---|---|
| Developer (more than one) | writes the fix; independent agents review each other's work |
| QA tester | decides whether a failing test or the code is wrong; writes the missing tests |
| Security | judges the change on what the security tools found |
| SRE | judges whether the change builds, deploys and stays healthy |
| Architect | judges whether the change fits the design of the system |
| Doc writer | keeps the documentation true to the code |

## Rules that do not bend

1. **No person in the loop, unless the owner asks to be in it.** Every change ends **merged**,
   **abandoned** (closed, with the reason) or **reverted**. By default nothing waits for a decision,
   an approval or a merge by a person, and a design that needs one is wrong. There is exactly one
   exception, and it is the owner's own dial: `risk_level`, 1 to 10. At 1, the default, this rule
   holds with no exception. Above 1 the owner has chosen to confirm the riskier changes by putting
   the label `risk-confirmed` on the pull request; at 10, every change. A change waiting for that
   label is the rule working as the owner set it, not a breach of it. Nothing else may make a change
   wait for a person.
2. **One LangGraph graph.** Every role is a node of the graph in `scripts/agent_pipeline.py`. No
   second engine, no model call outside the graph that judges or changes a pull request.
3. **Agents judge evidence, not diffs alone.** Each role reads the output of the deterministic tools
   the pipeline already runs (table below). A tool decides facts; a model decides what to do about
   them. Do not add a tool when one in the table already answers the question.
4. **What gates is whether the application works.** Tests, build and deploy gate a change. Security
   and quality tools (Trivy, Checkov, SBOM) give awareness and never block: a finding is a warning on
   the run and a section of its summary, and the change goes on. An agent that reads such a finding
   reports it; it does not hold the change for it.
5. **No over-engineering.** A change needs a defect seen in a real run, or a role in the table above
   that is missing. Prefer changing a node or a prompt over adding a workflow, a script, a tag or a
   variable. When something is replaced, delete what it replaces in the same change.
6. **Tests are the specification.** An agent changes a test only when it can quote the words of the
   pull request that redefine the behaviour. Otherwise the code is what gets fixed.

## Deterministic tools already in the pipeline

These exist in the consumer today. They are the evidence; use them before anything new.

| Tool | Where it runs | What it says | Read by an agent today |
|---|---|---|---|
| `pip install --dry-run` | PR `checks` | dependencies resolve on the image's interpreter | yes (check result, logs) |
| flake8 | PR `checks`, `main` build | syntax errors, undefined names | yes (check result, logs) |
| pytest (unit) | PR `checks`, `main` build | behaviour against the specification | yes |
| smoke test (boot + curl) | PR `checks` | the app starts and answers | yes (check result, logs) |
| pytest (integration) | PR `integration` | behaviour with PostgreSQL and Redis | yes (check result, logs) |
| docker build + kind + helm deploy | PR `image`, `main` `k8s-check` | the image builds, deploys, becomes ready | result only |
| actionlint | PR `workflows` | workflow files are valid | result only |
| Trivy (image, CRITICAL) | PR `image`, `main`; never fails; warning and run summary | known vulnerabilities in the image | yes, as awareness |
| Checkov (helm, SARIF) | PR `awareness`, `main`; never fails; warning and run summary | misconfiguration of the chart | yes, as awareness |
| TruffleHog | PR `awareness`, `main` `secrets`; never fails; warning and run summary | secrets in the commits (found or not, never the value) | yes, as awareness |
| SBOM (anchore) | `main` only, never fails | what the image contains | **no** |

## Roles and nodes: where we are

| Role | Node / prompt today | State |
|---|---|---|
| Developer | `writer`, `reviewer-correctness`, `final-reviewer` | in the graph |

The four reviewers (`reviewer-correctness`, `reviewer-security`, `reviewer-sre`, `reviewer-architect`) run in
parallel and independently. A critical or high finding on a changed line goes back to the developer agent,
once; the rest is a note, and so is what remains after that one fix. A reviewer's finding never ends a
change whose checks pass (rule 4). The architect only informs: its findings are always notes.

| QA tester | `test-steward`, `failure-adjudicator` | in the graph |
| Doc writer | `doc-reviewer` | in the graph |
| Security | `reviewer-security` | in the graph; reads the diff and the tools' evidence (`ai-context-*` artifacts of the commit), which it reports as awareness |
| SRE | `reviewer-sre` | in the graph; reads the diff and the deploy and scan evidence |
| Architect | `reviewer-architect` | in the graph; reads the diff and the project's own description (`--context-files`). `docs-architect` is still started by hand; `planner` belongs to the issue flow, which is out for now |

Known departures from the rules, to remove rather than extend:

- `scripts/pr_review_sweep.py` is a second engine for the dependency bot's pull requests.
- `reusable_ci-analysis.yml`, release notes and changelog are model calls outside the graph; the
  first one receives the Trivy, Checkov and deploy output and acts on none of it.
- A change to `.github/workflows/` cannot be merged by an agent (no `workflow` scope), so every
  change to the pipeline itself is merged by a person.

## What the pipeline must close by itself

The ordinary actions of a team on a repository. This is the acceptance list: the pipeline is done
when every row ends merged, abandoned or reverted with the named roles having judged it.
State: **yes** handled · **part** handled in part, outside the graph, or never seen in a real run ·
**no** not handled. Update the state in the change that moves it.

### Pull requests

| # | Action | Roles | State |
|---|---|---|---|
| 1 | New feature (code and tests) | developer, QA, security, architect, doc | yes |
| 2 | Bug fix | developer, QA (regression test) | yes |
| 3 | Intended change of behaviour (a test must change) | QA, developer | yes: the quote rule |
| 4 | Pull request with red CI | QA (test or code at fault?), developer | yes |
| 5 | Refactor with no change of behaviour | developer, architect | yes |
| 6 | Documentation only | doc writer | yes |
| 7 | Infrastructure: Dockerfile, helm chart, compose | SRE, security | part: both roles read the evidence; not yet seen on a real infrastructure change |
| 8 | Configuration, environment variables, database schema | architect, SRE | part: both roles review it; not yet seen in a real run |
| 9 | Change to the pipeline (`.github/workflows/`) | SRE | no: a person merges it |
| 10 | Pull request behind the base branch or in conflict | developer | part: only for the dependency bot |
| 11 | Stalled pull request (forgotten draft, author gone) | — | part: closed only when it has no verdict |
| 12 | Pull request from a fork (no secrets) | security | no: never tried |
| 13 | Very large pull request, or one across many areas | all | no: the case for the risk score |

### Directly on the main branch

| # | Action | Roles | State |
|---|---|---|---|
| 14 | Correct direct push | developer, QA | part: reviewed afterwards; the fix pull request never seen in a real run |
| 15 | Direct push that breaks the build | SRE | yes: reverted |
| 16 | Urgent hotfix | developer, SRE | yes: same path as 14 and 15 |
| 17 | Deploy that fails after the merge | SRE | yes: reverted, with no diagnosis |
| 18 | Force push or rewritten history | SRE | no |

### Dependencies

| # | Action | Roles | State |
|---|---|---|---|
| 19 | Patch or minor update of a library | developer, QA | yes, in the second engine (sweep) |
| 20 | Major update with a changed API | developer (migration) | yes, in the sweep |
| 21 | Base image or runtime | SRE, QA | part: one source for the version, not yet seen in a real run |
| 22 | Versions of GitHub Actions | SRE | part: reviewed, the bot's wait for `agent-review` not verified |
| 23 | Vulnerability found in a dependency already on the main branch (no pull request exists) | security | no: Trivy sees it, nobody acts |
| 24 | Secret committed | security | part: TruffleHog reports it on the run and to the security role; nothing removes it |

### Release

| # | Action | Roles | State |
|---|---|---|---|
| 25 | Version number (patch, minor, major) | architect | no: nobody decides it |
| 26 | Changelog and release notes | doc writer | part: automatic, outside the graph |
| 27 | Build and publish the image, SBOM | SRE | yes: deterministic |
| 28 | Update the deploy manifest | SRE | yes: deterministic |
| 29 | Rollback in production | SRE | no: outside the pipeline |

### Issues

| # | Action | Roles | State |
|---|---|---|---|
| 30 | Bug report to a fix pull request | QA, developer | no: removed by choice, to be decided |
| 31 | Feature request to a plan to a pull request | architect, developer | no: `planner` exists and is unused |
| 32 | Triage: duplicates, questions, labels | QA | no |
| 33 | Security report | security | no |

### Upkeep

| # | Action | Roles | State |
|---|---|---|---|
| 34 | Flaky test or runner failure | QA, SRE | yes: one re-run, then a verdict |
| 35 | The pipeline checks itself | SRE | yes: health check and canary |
| 36 | Model unavailable or budget exhausted | SRE | part: retried, then abandoned |
| 37 | Documentation drifting from the code with no pull request | doc writer, architect | no: started by hand |
| 38 | Technical debt: dead code, falling coverage | developer, QA | no |

The rows in state **no** gather on three roles: security (7, 12, 23, 24, 33), SRE (9, 18, 29) and
architect (8, 25, 31, 37). Those are the roles the graph lacks.

## Order of work

Each step reuses or replaces something; none adds a layer.

1. Done: Trivy, Checkov and TruffleHog output is evidence for `reviewer-security`, which reports what
   they found without blocking (rule 4). Still to do: remove `ci-analysis`.
2. Done: `reviewer-sre` reads the build and deploy evidence of the `image` job.
3. Done: `reviewer-architect` reviews every change.
4. The dependency bot's pull requests go through the same graph; the sweep only picks the pull
   request and starts it.
5. Optional: a risk score computed from facts (files touched, size, tool findings) that chooses
   between merge and abandon.

## Layout

- `scripts/agent_pipeline.py` — the graph. `scripts/agent_lib.py` — what the nodes share.
- `prompts/agents/` — one prompt per role.
- `.github/workflows/reusable_*.yml` — what a consumer calls. A consumer's workflow only wires
  inputs; logic lives here.
- `scripts/pipeline_health.py`, `scripts/pipeline_canary.py` — the loop checking itself.
- `docs/architecture.md` — the system as it is. Keep it true in the same change.

## Working here

- Tests: `pytest tests/` (needs `scripts/requirements-autofix.txt`). The unit tests do not show what
  a model does with a real pull request: after an engine change, the consumer's canary is the proof.
- Releases: `v2` moves by itself after the tests and the consumer's canary pass. Never move it by hand.
- A reusable workflow that needs a new permission or input: the consumer's caller is changed first.
- Say what was verified in a real run and what was not. Do not describe behaviour as working because
  the code for it exists.
