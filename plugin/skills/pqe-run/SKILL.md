---
name: pqe-run
description: Run the Project Quality Engine on the current project and explain the result — release decision, goals ticked or not, findings by severity and module, and what to do next.
---

# Run PQE and interpret it

```bash
pqe run . --setup
```

Then read the newest `.qa/out/<timestamp>/report.md` (and `report.json` for detail) and answer:

1. **Decision** (GO / CONDITIONAL / NO_GO) and the exact reasons.
2. **Goals**: which are MET, NOT_MET, PARTIAL, UNVERIFIED — and for each non-MET goal the failing criterion.
3. **Findings**: SEV0/SEV1 first, grouped by architecture module, NEW vs EXISTING relative to the baseline.
4. **Blind spots**: dimensions that were SKIPPED or NOT_COVERED, and why.
5. **Next actions**: fixes (point to `fix-briefs/`), thresholds to calibrate, attestations needed.

Never describe a SKIPPED or UNCALIBRATED check as passed.
