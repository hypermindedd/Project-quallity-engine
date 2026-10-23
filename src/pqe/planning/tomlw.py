"""Minimal TOML writer for generated contracts.

Supports exactly what contracts use: strings, booleans, ints, floats, lists of
scalars or inline tables, nested tables and arrays of tables. Output is
re-parsed with :mod:`tomllib` by the caller, so a writer bug cannot produce a
contract that silently means something else.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE.match(key) else json.dumps(key, ensure_ascii=False)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_scalar(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


#: Lists of tables written inline (short); every other list of tables becomes [[array.of.tables]].
INLINE_LISTS = {"criteria", "viewports"}


def _is_table_array(key: str, value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value) and key not in INLINE_LISTS


def _emit(table: dict[str, Any], path: list[str], out: list[str], comments: dict[str, str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict) and not _is_table_array(k, v)}
    for key, value in scalars.items():
        out.append(f"{_key(key)} = {_scalar(value)}")
    for key, value in table.items():
        if key in scalars:
            continue
        sub = [*path, key]
        name = ".".join(_key(p) for p in sub)
        if isinstance(value, dict):
            out.append("")
            if name in comments:
                out.append(f"# {comments[name]}")
            out.append(f"[{name}]")
            _emit(value, sub, out, comments)
        else:
            for item in value:
                out.append("")
                out.append(f"[[{name}]]")
                _emit(item, sub, out, comments)


def dumps(data: dict[str, Any], header: str = "", comments: dict[str, str] | None = None) -> str:
    out: list[str] = [f"# {line}" if line else "#" for line in header.splitlines()]
    if out:
        out.append("")
    _emit(data, [], out, comments or {})
    return "\n".join(out).strip() + "\n"
