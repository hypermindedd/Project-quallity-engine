"""Fix brief: everything a coding agent needs to fix one finding, and nothing more.

The engine does not edit code itself. It hands a bounded brief to an agent
(Claude Code skill ``diamond-fix``), which must: confirm the root cause in the
code, fix it inside the project's existing architecture, and prove the fix by
re-running the check. Briefs keep token cost low: one finding, its evidence,
its likely files — not the whole session.
"""

from __future__ import annotations

import json

from ..config import ModelRouting
from ..model import Category, Finding


def route_task(f: Finding) -> str:
    """Pick the task type (→ model tier) for fixing this finding."""
    if f.category == Category.ARCHITECTURE:
        return "architecture_review"
    files = {s.path for s in f.sources}
    strong = any(s.confidence >= 0.8 for s in f.sources)
    if f.category in (Category.CONSISTENCY,) or len(files) > 2 or not strong:
        return "root_cause_cross_file"
    return "root_cause_single_file"


def fix_brief(f: Finding, routing: ModelRouting) -> str:
    task = route_task(f)
    target = {k: (f.target or {}).get(k) for k in ("selector", "tag", "text", "styles", "component") if (f.target or {}).get(k)}
    sources = "\n".join(f"- {s.render()}  ({s.method}, confidence {s.confidence:.1f})" for s in f.sources) or "- (none resolved)"
    return f"""# Fix brief {f.fingerprint}

- Task type: {task}
- Suggested model: {routing.model_for(task)}
- Severity / category / origin: {f.severity.value} / {f.category.value} / {f.origin.value}

## Problem
{f.title}

{f.detail}

Reviewer said (Persian, verbatim): {f.utterance or "-"}

## Where
- Page: {f.page_url or "-"}
- Screenshot: {f.screenshot or "-"}

Element:
```json
{json.dumps(target, ensure_ascii=False, indent=2)}
```

Source candidates (verify before editing; confidence < 0.8 is a heuristic guess):
{sources}

## Evidence
```json
{json.dumps(f.evidence, ensure_ascii=False, indent=2, default=str)}
```

## Suggested direction
{f.suggestion or "-"}

## Rules
1. Confirm the root cause in code before changing anything; write the reason in the PR.
2. Fix at the root (component / token / shared style), not with a page-specific patch or `!important`.
3. Stay inside the project's existing architecture and design tokens; do not add dependencies without a reason.
4. Never edit tests or the quality contract to make a check pass.
5. Prove it: re-run `diamond analyze` on a fresh session (or the generated regression test) and show
   that finding `{f.fingerprint}` is gone and no new SEV0–SEV2 finding appeared.
"""
