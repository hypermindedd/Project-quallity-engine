---
name: diamond-fix
description: Fix one or more Diamond review findings at their root cause, inside the project's architecture, and prove the fix. Use after a Diamond review when the user asks to fix findings.
---

# Diamond fix

For each finding id:

1. Get the brief: `diamond_fix_brief(session, id)` (or read `briefs/<id>.md`).
2. Use the model tier the brief suggests. Escalate to the heavy tier only when the root cause
   spans several files or the source candidates are all below 0.8 confidence.
3. **Confirm the root cause in code before editing.** Candidates below 0.8 are guesses: open
   them, find the real origin (shared component, token, global style), and write down why.
4. Fix at the root:
   - consistency findings → design tokens / shared component, not per-page overrides
   - no `!important`, no page-specific patches, no new dependencies without a stated reason
   - never edit tests or quality contracts to make a check pass
5. Work on a branch, one commit per finding (message: `fix(diamond:<id>): <what and why>`).
6. Prove it: re-run the review (or the generated regression test for that finding) and show the
   finding is gone and no new SEV0–SEV2 finding appeared. If you cannot prove it, say so.
7. Open a PR whose body lists, per finding: root cause, change, proof.
