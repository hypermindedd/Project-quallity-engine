---
name: pqe-onboard
description: Connect a repository to the Project Quality Engine and review what it designed — run the autonomous mode, read the plan, accept or correct AI-proposed goals, and take ownership of the contract. Use when a project is new to PQE or its generated contract needs review.
---

# Onboard a project into PQE

The engine designs the tests itself. Your job is to **review**, not to write the contract by hand.

1. Run the autonomous mode (installs dependencies, explores the running app, designs and runs every test):
   ```bash
   pqe auto .
   ```
   Without `ANTHROPIC_API_KEY` add `--no-ai`; the deterministic plan still covers every universal check.
2. Read `.qa/plan.md` (what was planned and why), `.qa/classification.json` (what the engine thinks the project is)
   and `.qa/planner.json` (what the AI proposed and what was rejected).
3. Check the classification against the project's documents. If a component was missed or misread,
   fix it in `.qa/pqe.toml` (start command, health path, sandbox env, pages, test-account bootstrap in `[[ui.actions]]`).
4. Review every goal marked `proposed = true`: compare it with the cited document section.
   - Correct → remove `proposed`; mark it `hard_gate = true` when the documents call it release-critical.
   - Wrong or unsupported → delete it. Never weaken a correct expectation to match the code.
5. Declare architecture boundaries from the architecture documents in `[[architecture.rules]]`.
6. Delete the `pqe:generated` line at the top of `.qa/pqe.toml`. From then on the engine never overwrites it.
7. Commit `.qa/` and add the CI file:
   ```bash
   cp <engine>/templates/project-workflow.yml .github/workflows/pqe.yml
   ```

Never mark a SKIPPED, UNCALIBRATED or UNVERIFIED result as passed. Thresholds (latency, Lighthouse, visual diff)
are set only after a human approves a baseline with `pqe baseline`.
