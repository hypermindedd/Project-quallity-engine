"""Publish a run summary to the Notion command room.

Goals are written as Notion to-do blocks, so the page literally shows which
project goals are ticked (MET) and which are not. Requires an internal
integration token (``report.notion.token_env``) shared with the parent page.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import NotionSpec

API = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"
MAX_CHILDREN = 100

Transport = Callable[[str, dict[str, str], bytes], dict[str, Any]]


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content[:1900]}}]


def build_page(report: dict[str, Any], parent_page_id: str) -> dict[str, Any]:
    decision = report["decision"]["decision"]
    icon = {"GO": "✅", "CONDITIONAL": "⚠️", "NO_GO": "⛔"}[decision]
    children: list[dict[str, Any]] = [
        {"object": "block", "type": "callout", "callout": {"rich_text": _text(f"{decision} — " + "; ".join(report["decision"]["reasons"])), "icon": {"emoji": icon}}},
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _text("اهداف پروژه")}},
    ]
    for goal in report["goals"]:
        label = f"{goal['goal_id']} — {goal['title']} [{goal['status']}]" + (" (گیت سخت)" if goal["hard_gate"] else "")
        children.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _text(label), "checked": goal["status"] == "MET"}})
    children.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": _text("چک‌ها")}})
    for check in report["checks"]:
        children.append({"object": "block", "type": "bulleted_list_item",
                         "bulleted_list_item": {"rich_text": _text(f"{check['status']} · {check['check_id']} · {check['summary']}")}})
    return {
        "parent": {"page_id": parent_page_id},
        "icon": {"emoji": icon},
        "properties": {"title": {"title": _text(f"PQE · {report['project']} · {report['finished_at'][:16]} · {decision}")}},
        "children": children[:MAX_CHILDREN],
    }


def _urllib_transport(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)  # noqa: S310  # nosec B310 - fixed https endpoint
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310  # nosec B310 - fixed https endpoint
        return json.loads(resp.read())


def publish(report: dict[str, Any], spec: NotionSpec, transport: Transport = _urllib_transport) -> str:
    """Create the page; returns its URL. Raises RuntimeError with a clear reason."""
    token = os.environ.get(spec.token_env, "")
    if not token:
        raise RuntimeError(f"environment variable {spec.token_env} is not set")
    if not spec.parent_page_id:
        raise RuntimeError("report.notion.parent_page_id is empty")
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    try:
        answer = transport(API, headers, json.dumps(build_page(report, spec.parent_page_id)).encode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Notion rejected the page: HTTP {exc.code} {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Notion unreachable: {exc.reason}") from exc
    return str(answer.get("url", ""))
