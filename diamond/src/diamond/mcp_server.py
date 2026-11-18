"""MCP server: lets any MCP-capable AI (Claude, Cursor, …) drive Diamond.

Tools:
  diamond_start_review(url, repo?, audio?)  → opens the browser, starts recording
  diamond_stop_review()                     → stops and analyzes, returns summary
  diamond_analyze(session, repo?)           → (re)analyze a recorded session
  diamond_findings(session, max_severity?)  → compact finding list
  diamond_fix_brief(session, finding_id)    → the brief for one finding

Works with the MCP Python SDK v2 (MCPServer) and v1 (FastMCP).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_config
from .pipeline import analyze
from .recorder import ReviewSession

try:  # MCP SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - SDK v1
    try:
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError as exc:
        raise SystemExit("MCP SDK missing: pip install 'diamond-engine[mcp]'") from exc

server = _Server("diamond")
_active: dict[str, ReviewSession] = {}
SEV_ORDER = ["SEV0", "SEV1", "SEV2", "SEV3", "INFO"]


@server.tool()
def diamond_start_review(url: str, repo: str = "", audio: bool = True) -> str:
    """Open a browser on `url` and record a narrated review (voice + pointer + page telemetry)."""
    if "current" in _active:
        return "A review is already running; call diamond_stop_review first."
    repo_path = Path(repo).resolve() if repo else None
    session = ReviewSession(load_config(repo_path), url, repo=repo_path, audio=audio)
    session.start()
    _active["current"] = session
    return json.dumps({"session": str(session.store.root), "microphone": session.audio_status})


@server.tool()
def diamond_stop_review() -> str:
    """Stop the running review and analyze it. Returns the summary and report path."""
    session = _active.pop("current", None)
    if session is None:
        return "No review is running."
    root = session.stop()
    repo = Path(session.store.meta["repo"]) if session.store.meta.get("repo") else None
    return json.dumps(analyze(root, load_config(repo), repo).summary(), ensure_ascii=False)


@server.tool()
def diamond_analyze(session: str, repo: str = "") -> str:
    """Analyze (or re-analyze) a recorded session directory."""
    repo_path = Path(repo).resolve() if repo else None
    return json.dumps(analyze(Path(session), load_config(repo_path), repo_path).summary(), ensure_ascii=False)


@server.tool()
def diamond_findings(session: str, max_severity: str = "SEV3") -> str:
    """Compact list of findings at or above `max_severity` (SEV0..SEV3, INFO)."""
    data = json.loads((Path(session) / "findings.json").read_text(encoding="utf-8"))
    limit = SEV_ORDER.index(max_severity) if max_severity in SEV_ORDER else 3
    rows = [{"id": f["id"], "severity": f["severity"], "category": f["category"], "title": f["title"],
             "origin": f["origin"], "sources": [s["path"] + (f":{s['line']}" if s["line"] else "") for s in f["sources"][:2]]}
            for f in data["findings"] if SEV_ORDER.index(f["severity"]) <= limit]
    return json.dumps(rows, ensure_ascii=False)


@server.tool()
def diamond_fix_brief(session: str, finding_id: str) -> str:
    """The fix brief (problem, evidence, likely source files, model tier, acceptance rule) for one finding."""
    path = Path(session) / "briefs" / f"{finding_id}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else f"No brief for {finding_id}"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
