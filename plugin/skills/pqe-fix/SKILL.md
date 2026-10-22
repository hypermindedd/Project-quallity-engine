---
name: pqe-fix
description: Repair defects found by the Project Quality Engine on a dedicated branch, inside the project's architecture, without touching tests or the quality contract.
---

# Repair loop

Either let the engine drive Claude Code headlessly:

```bash
pqe fix . --max-iterations 3
```

or work through the briefs yourself: open `.qa/out/<latest>/fix-briefs/*.md` and, for each module:

1. Read the finding, its location and the architecture rules in the brief.
2. Fix the **root cause** in the owning module. Minimal diff, project style.
3. Never edit protected paths (tests, `.qa/`, baselines, authority documents). If a fix seems to need that,
   stop and explain — the oracle is not negotiable.
4. Re-run only the affected checks: `pqe run . --only <check-id>`.
5. Commit per module on the `pqe/fix-*` branch. A human reviews and merges.
