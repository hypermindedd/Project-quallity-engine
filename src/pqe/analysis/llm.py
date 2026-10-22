"""Optional advisory visual review by a vision-capable model.

Subjective design quality cannot be measured deterministically, so this check
is *advisory*: its findings are SEV3/INFO and can never block a release. It
runs only when ``ui.llm_review = true`` and ``ANTHROPIC_API_KEY`` is set.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from ..context import RunContext
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_IMAGES = 6

PROMPT = """You are a senior product designer reviewing screenshots of a web application.
Project goal: {goal}
For each screenshot, list only concrete, visible defects: overlapping or clipped text, misaligned or
inconsistent spacing, unreadable contrast, broken right-to-left layout, truncated controls, inconsistent
typography, elements that look broken or unfinished. Do not praise. Do not speculate about code.
Answer ONLY with JSON: {{"issues": [{{"screen": <index>, "severity": "minor"|"moderate", "issue": "<one sentence>"}}]}}"""


def review_screens(ctx: RunContext, shots: list[tuple[str, str, Path]]) -> CheckResult:
    cid = "ui.design_review"
    title = "Advisory design review (vision model)"
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return CheckResult.skipped(cid, title, Dimension.UI_VISUAL, "ANTHROPIC_API_KEY not set")
    selected = shots[:MAX_IMAGES]
    content: list[dict] = []
    for index, (viewport, url, path) in enumerate(selected):
        content.append({"type": "text", "text": f"Screen {index}: {url} at {viewport}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": base64.b64encode(path.read_bytes()).decode()}})
    content.append({"type": "text", "text": PROMPT.format(goal=ctx.config.project.goal or ctx.config.project.name)})
    body = json.dumps({"model": os.environ.get("PQE_REVIEW_MODEL", DEFAULT_MODEL), "max_tokens": 2000,
                       "messages": [{"role": "user", "content": content}]}).encode()
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={  # nosec B310 - constant https URL
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310  # nosec B310 - fixed https endpoint
            payload = json.loads(resp.read())
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        return CheckResult.errored(cid, title, Dimension.UI_VISUAL, f"review request failed: {exc}")
    text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    try:
        issues = json.loads(text[start:end + 1]).get("issues", []) if start >= 0 else []
    except ValueError:
        return CheckResult.errored(cid, title, Dimension.UI_VISUAL, "model answer was not valid JSON")
    result = CheckResult(cid, title, Dimension.UI_VISUAL, Status.PASS)
    for issue in issues:
        idx = issue.get("screen", 0)
        viewport, url, _ = selected[idx] if isinstance(idx, int) and 0 <= idx < len(selected) else ("?", "?", None)
        result.findings.append(Finding(cid, Dimension.UI_VISUAL,
                                       Severity.SEV3 if issue.get("severity") == "moderate" else Severity.INFO,
                                       f"[advisory] {issue.get('issue', '')[:200]}", location=Location(url),
                                       evidence={"key": f"{url}:{viewport}:{issue.get('issue', '')[:60]}", "advisory": True}))
    result.summary = f"{len(result.findings)} advisory observation(s); never blocks a release"
    return result
