---
name: pqe-onboard
description: Onboard a project into the Project Quality Engine — read its documents, write the .qa/pqe.toml quality contract (goals, architecture rules, API scenarios, UI flow) and author missing tests derived from the documents. Use when a new project needs automated QA or its contract is a fresh draft.
---

# Onboard a project into PQE

The contract you write is the **test oracle**. Derive every expectation from the project's
documents (charter, specs, architecture, QA plan, README), never from what the code currently does.
A test copied from current behaviour only proves the code does what it does.

## Steps

1. Run discovery and the draft:
   ```bash
   pqe init .
   ```
   Read `.qa/profile.json`: languages, frameworks, entrypoints, requirement families, goal statements.
2. Read the authority documents end to end. Identify:
   - the product goal (one sentence, with document reference);
   - requirement identifier families (e.g. `C-01`, `TS-07`) → `[[trace_sources]]` with a regex;
   - hard gates (anything the documents call release-blocking, safety, privacy, isolation);
   - architecture boundaries (who may import whom) → `[[architecture.rules]]`, module owners → `[[architecture.modules]]`.
3. For each goal write `[[goals]]` with `covers` (requirement IDs) and **evidence**:
   - `tests` criteria selecting existing test IDs (`tests/test_x.py::test_*`), with a sensible `min_tests`;
   - `check` criteria for engine checks (`api.scenario.<id>`, `security.secrets`, `ui.interaction`, …);
   - `attestation` only for what genuinely needs a human (live provider, visual acceptance, red team).
4. API: enable `[api]` with a `start_command` that boots an **isolated** instance (generated secrets,
   `temp_dirs` for data). Declare endpoints and at least one goal-level `[[api.scenarios]]` flow.
5. UI: enable `[ui]` with a start command, `expect_dir` (`rtl` for Persian), and `[[ui.actions]]`
   to reach the app behind its sign-in using a test account. Never point at production.
6. Gaps: for every goal that has no executable evidence, write the missing tests **from the documents**
   in the project's own test style, then reference them in the goal. Do not weaken existing tests.
7. Do not invent thresholds. Leave `[performance]`/`[load]` limits empty until a human approves a baseline.
8. Run `pqe run . --setup` and iterate on the *contract* (selectors, start commands) until every check
   either runs or is SKIPPED for a stated reason. Report findings honestly; do not tune expectations to make them pass.

Deliver: the contract, any new tests, and a short summary of goals by status (MET / NOT_MET / UNVERIFIED).
