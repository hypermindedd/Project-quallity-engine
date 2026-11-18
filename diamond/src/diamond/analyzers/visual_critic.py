"""Optional vision-model critique of the moments the reviewer talked about.

Opt-in (``visual_critic = true``) because it costs tokens. Only screenshots
attached to VISUAL/LAYOUT/CONSISTENCY voice notes are sent (bounded), with
the element descriptor and the reviewer's words, to the *standard* tier.
Output is advisory: it never raises severity above SEV2.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register

MAX_IMAGES = 6
API_URL = "https://api.anthropic.com/v1/messages"
PROMPT = (
    "You are a senior product designer reviewing a web page screenshot. The reviewer said (Persian): «{said}». "
    "The element they pointed at: {target}. Identify concrete visual problems (alignment, spacing, hierarchy, colour, "
    "typography, clutter, redundant buttons) and the most likely CSS-level cause. Reply ONLY with JSON: "
    '{{"issues":[{{"title_fa":"...","why_fa":"...","css_fix":"...","severity":"SEV2|SEV3|INFO"}}]}}'
)


def call_claude(api_key: str, model: str, image_b64: str, prompt: str) -> dict:
    body = json.dumps({
        "model": model, "max_tokens": 1024,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": prompt}]}],
    }).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            API_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed https endpoint
        data = json.loads(resp.read())
    text = "".join(block.get("text", "") for block in data.get("content", []))
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0)) if match else {"issues": []}


@register
class VisualCriticAnalyzer:
    name = "visual_critic"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        if not ctx.cfg.visual_critic:
            return AnalyzerResult.skipped(self.name, "disabled (set visual_critic = true to enable; uses tokens)")
        if not ctx.cfg.anthropic_api_key:
            return AnalyzerResult.skipped(self.name, f"{ctx.cfg.anthropic_api_key_env} is not set")
        model = ctx.cfg.routing.model_for("visual_critique")
        result = AnalyzerResult(self.name, Status.PASS)
        wanted = {Category.VISUAL, Category.LAYOUT, Category.CONSISTENCY}
        sent = 0
        for note in ctx.notes:
            if note.category not in wanted or sent >= MAX_IMAGES:
                continue
            shot = ctx.store.shot_near(note.anchor)
            if not shot:
                continue
            image = base64.b64encode((ctx.store.root / shot).read_bytes()).decode()
            target = json.dumps({k: (note.target or {}).get(k) for k in ("tag", "text", "selector", "styles")}, ensure_ascii=False)[:1500]
            reply = call_claude(ctx.cfg.anthropic_api_key, model, image, PROMPT.format(said=note.text, target=target))
            sent += 1
            for issue in reply.get("issues", [])[:4]:
                sev = Severity(issue.get("severity", "SEV3")) if issue.get("severity") in ("SEV2", "SEV3", "INFO") else Severity.SEV3
                result.findings.append(Finding(
                    check_id="visual.critique", category=Category.VISUAL, severity=sev,
                    title=issue.get("title_fa", "")[:120], detail=issue.get("why_fa", ""),
                    page_url=note.page_url, target=note.target, utterance=note.text, ts=note.anchor, screenshot=shot,
                    evidence={"key": f"{int(note.anchor)}:{issue.get('title_fa', '')[:40]}", "model": model, "advisory": True},
                    suggestion=issue.get("css_fix", ""),
                ))
        result.metrics = {"images_sent": sent, "model": model}
        return result
