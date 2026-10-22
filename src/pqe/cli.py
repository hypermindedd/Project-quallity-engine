"""Command-line interface.

    pqe discover [PATH]            what is this project? (read-only)
    pqe init [PATH]                write a draft .qa/pqe.toml contract
    pqe run [PATH]                 run every applicable check, verify goals, decide
    pqe fix [PATH]                 bounded repair loop on a new branch (human merges)
    pqe baseline [PATH] REPORT     accept a run as the regression baseline
    pqe doctor                     which tools are available here
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_DIR, CONFIG_FILE, ConfigError, load_config
from .discovery.draft import draft_contract
from .discovery.profile import discover
from .orchestrator import RunOptions, exit_code, run_pipeline

TOOLS = ["git", "python3", "ruff", "bandit", "pip-audit", "semgrep", "node", "npm", "pnpm", "k6", "claude", "gh"]


def _root(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise SystemExit(f"not a directory: {path}")
    return path


def cmd_discover(args: argparse.Namespace) -> int:
    profile = discover(_root(args.path))
    print(profile.to_json())
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = _root(args.path)
    target = root / CONFIG_DIR / CONFIG_FILE
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite).", file=sys.stderr)
        return 2
    profile = discover(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(draft_contract(profile), encoding="utf-8")
    (target.parent / "profile.json").write_text(profile.to_json(), encoding="utf-8")
    gitignore = target.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("out/\n", encoding="utf-8")
    print(f"Draft contract written to {target}.")
    print("Next: map goals to evidence (Claude Code: /pqe:pqe-onboard), then run `pqe run`.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(_root(args.path))
    options = RunOptions(only=args.only, skip=args.skip, out_dir=Path(args.out).resolve() if args.out else None,
                         setup=args.setup, publish=args.publish, fail_on=args.fail_on)
    report = run_pipeline(config, options)
    decision = report["decision"]
    print(f"\n{decision['decision']}: " + "; ".join(decision["reasons"]))
    for goal in report["goals"]:
        print(f"  [{goal['status']:>10}] {goal['goal_id']} {goal['title']}")
    print(f"\nReport: {report['out_dir']}/report.md")
    if report.get("notion_url"):
        print(f"Notion: {report['notion_url']}")
    return exit_code(report, args.fail_on)


def cmd_fix(args: argparse.Namespace) -> int:
    from .remediation.loop import RemediationError, remediate

    config = load_config(_root(args.path))
    if args.max_iterations:
        config.remediation.max_iterations = args.max_iterations
    try:
        outcome = remediate(config, RunOptions(only=args.only, skip=args.skip), push=args.push)
    except RemediationError as exc:
        print(f"remediation aborted: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(outcome.to_dict(), indent=2))
    return 0 if outcome.final_decision != "NO_GO" else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    """Accept a reviewed run as the new baseline (an explicit human decision)."""
    root = _root(args.path)
    report_path = Path(args.report).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    base = root / CONFIG_DIR / "baselines"
    base.mkdir(parents=True, exist_ok=True)
    fingerprints = sorted({f["fingerprint"] for c in report["checks"] for f in c["findings"]})
    (base / "findings.json").write_text(json.dumps({"accepted_from": report["finished_at"], "fingerprints": fingerprints}, indent=2), encoding="utf-8")
    perf = report_path.parent / "perf-observed.json"
    if perf.exists():
        shutil.copyfile(perf, base / "perf.json")
    shots = report_path.parent / "ui"
    if shots.is_dir() and args.include_screens:
        shutil.copytree(shots, base / "ui", dirs_exist_ok=True)
    print(f"Baseline updated in {base} ({len(fingerprints)} known finding(s)). Review and commit it.")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    for tool in TOOLS:
        print(f"{'✓' if shutil.which(tool) else '✗'} {tool}")
    print(f"{'✓' if importlib.util.find_spec('playwright') else '✗'} playwright (python package)")
    print(f"{'✓' if importlib.util.find_spec('PIL') else '✗'} Pillow (visual diff)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pqe", description="Project Quality Engine")
    parser.add_argument("--version", action="version", version=f"pqe {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="profile a project (read-only)")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("init", help="write a draft quality contract")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    for name, func, help_text in (("run", cmd_run, "run all checks"), ("fix", cmd_fix, "bounded repair loop on a branch")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", nargs="?", default=".")
        p.add_argument("--only", action="append", default=[], help="runner id or dimension (repeatable)")
        p.add_argument("--skip", action="append", default=[], help="runner id or dimension (repeatable)")
        p.set_defaults(func=func)
    run_p = sub.choices["run"]
    run_p.add_argument("--out", help="output directory (default .qa/out/<timestamp>)")
    run_p.add_argument("--setup", action="store_true", help="run the contract's setup commands first")
    run_p.add_argument("--publish", action="store_true", help="publish the summary to Notion if configured")
    run_p.add_argument("--fail-on", choices=["no_go", "conditional"], default="no_go")
    fix_p = sub.choices["fix"]
    fix_p.add_argument("--max-iterations", type=int)
    fix_p.add_argument("--push", action="store_true", help="push the fix branch when it contains changes")

    p = sub.add_parser("baseline", help="accept a run as the regression baseline")
    p.add_argument("path")
    p.add_argument("report", help="path to report.json of the reviewed run")
    p.add_argument("--include-screens", action="store_true", help="also approve UI screenshots")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("doctor", help="show available tools")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
