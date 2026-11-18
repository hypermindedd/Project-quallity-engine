"""DOM element → ``file:line``. Strategies, strongest first:

1. **Build-time instrumentation** (confidence 1.0): an attribute such as
   ``data-insp-path="src/app/page.tsx:42:7:button"`` injected in dev builds by
   code-inspector-plugin (React/Next/Vue/Svelte, Vite/Webpack) or any tool
   that writes ``file:line[:col]``. Framework-agnostic and survives React 19.
2. **Framework dev metadata** (0.9): React ≤18 ``_debugSource``, Vue ``__file``,
   Svelte ``__svelte_meta``.
3. **Source maps** (0.3) for long-frame script URLs in a local Next.js build.
4. **Repository search** (0.3–0.6): visible text, ``id``, distinctive class
   names in components and stylesheets. Always labelled as heuristic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..analyzers.architecture import iter_source_files
from ..model import Finding, SourceLocation

_LOC = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?(?::[\w-]+)?$")
_UTILITY_CLASS = re.compile(r"^(?:[a-z]+:)+|^-?(?:p|m|px|py|pt|pb|pl|pr|mx|my|mt|mb|ml|mr|w|h|min|max|text|bg|flex|grid|gap|items|justify|"
                            r"rounded|border|shadow|font|leading|tracking|top|left|right|bottom|inset|z|opacity|block|inline|hidden|"
                            r"relative|absolute|fixed|sticky|overflow|cursor|transition|duration|ease|space|col|row|self|place|order)"
                            r"(?:-|$)")
_HASHED = re.compile(r"(?:__|_)[A-Za-z0-9]{5}$|^css-|^sc-|^jsx-")


def parse_location_string(value: str, repo: Path | None) -> SourceLocation | None:
    m = _LOC.match(value.strip())
    if not m:
        return None
    path = m.group("path")
    if repo is not None:
        try:
            path = str(Path(path).resolve().relative_to(repo.resolve())) if Path(path).is_absolute() else path
        except ValueError:
            pass
    return SourceLocation(path, int(m.group("line")), int(m.group("col")) if m.group("col") else None, 1.0, "attribute")


class SourceIndex:
    """Lazy, bounded in-memory index of the repo's UI source files."""

    def __init__(self, repo: Path | None) -> None:
        self.repo = repo
        self._files: dict[str, list[str]] | None = None

    @property
    def files(self) -> dict[str, list[str]]:
        if self._files is None:
            self._files = {}
            if self.repo and self.repo.is_dir():
                for path in iter_source_files(self.repo):
                    try:
                        self._files[str(path.relative_to(self.repo))] = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                    except OSError:
                        continue
        return self._files

    def search(self, needle: str, suffixes: tuple[str, ...] | None = None, limit: int = 5) -> list[tuple[str, int]]:
        hits: list[tuple[str, int]] = []
        for rel, lines in self.files.items():
            if suffixes and not rel.endswith(suffixes):
                continue
            for i, line in enumerate(lines, 1):
                if needle in line:
                    hits.append((rel, i))
                    if len(hits) >= limit:
                        return hits
        return hits


def _from_hints(source: dict, repo: Path | None) -> list[SourceLocation]:
    out: list[SourceLocation] = []
    attr = source.get("attr")
    if attr and (loc := parse_location_string(attr["value"], repo)):
        out.append(SourceLocation(loc.path, loc.line, loc.column, 1.0 if attr.get("depth", 0) == 0 else 0.8,
                                  f"attribute:{attr['name']}"))
    for key, method in (("react", "react-debug-source"), ("svelte", "svelte-meta"), ("vue", "vue-file")):
        info = source.get(key)
        if info and info.get("file"):
            path = info["file"]
            if repo is not None and Path(path).is_absolute():
                try:
                    path = str(Path(path).resolve().relative_to(repo.resolve()))
                except ValueError:
                    pass
            out.append(SourceLocation(path, info.get("line"), info.get("column"), 0.9, method))
    return out


def _from_sourcemap(script_url: str, repo: Path | None) -> list[SourceLocation]:
    """Next.js: /_next/static/chunks/app/page-abc.js → .next/static/chunks/app/page-abc.js.map → sources."""
    if repo is None or "/_next/" not in script_url:
        return []
    rel = urlsplit(script_url).path.split("/_next/", 1)[1]
    map_path = repo / ".next" / (rel + ".map")
    if not map_path.is_file():
        return []
    try:
        sources = json.loads(map_path.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, ValueError):
        return []
    own = [s.split("webpack://_N_E/", 1)[-1].lstrip("./") for s in sources if "node_modules" not in s and not s.startswith("webpack/")]
    return [SourceLocation(s, None, None, 0.3, "sourcemap") for s in own[:5]]


def _from_search(target: dict, index: SourceIndex) -> list[SourceLocation]:
    out: list[SourceLocation] = []
    components = (".tsx", ".jsx", ".vue", ".svelte", ".astro", ".html", ".ts", ".js")
    if target.get("id"):
        for rel, line in index.search(f'id="{target["id"]}"', components, 3) or index.search(f"id='{target['id']}'", components, 3):
            out.append(SourceLocation(rel, line, None, 0.6, "search:id"))
    text = (target.get("text") or "").strip()
    if 4 <= len(text) and not out:
        needle = text[:40]
        hits = index.search(needle, components, 5)
        conf = 0.5 if len(hits) == 1 else 0.3
        out.extend(SourceLocation(rel, line, None, conf, "search:text") for rel, line in hits)
    for cls in target.get("classes", []):
        if _UTILITY_CLASS.match(cls) or _HASHED.search(cls) or len(cls) < 4:
            continue
        for rel, line in index.search(f".{cls}", (".css", ".scss", ".sass", ".less"), 2):
            out.append(SourceLocation(rel, line, None, 0.4, "search:class"))
        if len(out) >= 6:
            break
    return out


def locate_finding(finding: Finding, index: SourceIndex) -> None:
    """Fill ``finding.sources`` in place (keeps what analyzers already found)."""
    found: list[SourceLocation] = list(finding.sources)
    target = finding.target or {}
    found.extend(_from_hints(target.get("source") or {}, index.repo))
    for src in list(found):
        if src.method == "loaf-script":
            found.extend(_from_sourcemap(src.path, index.repo))
    if not any(s.confidence >= 0.8 for s in found) and target:
        found.extend(_from_search(target, index))
    unique: dict[tuple, SourceLocation] = {}
    for s in found:
        unique.setdefault((s.path, s.line), s)
    finding.sources = sorted(unique.values(), key=lambda s: -s.confidence)[:6]
