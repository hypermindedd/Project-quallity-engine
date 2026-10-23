"""Command-line interface.

    pqe auto [PATH]                added to a repo? do everything: classify, plan, design, test, report
    pqe classify [PATH]            what kind of software is this, and how does it start?
    pqe plan [PATH]                which tests does it need, and where do they run?
    pqe discover [PATH]            raw project profile (read-only)
    pqe init [PATH]                write the generated contract and plan, run nothing
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
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_DIR, CONFIG_FILE, ConfigError, load_config
from .discovery.profile import discover
from .orchestrator import RunOptions, exit_code, run_pipeline

TOOLS = ["git", "python3", "ruff", "bandit", "pip-audit", "semgrep", "node", "npm", "npx", "pnpm", "claude", "gh",
         "adb", "emulator", "maestro", "flutter", "xcodebuild", "java"]


def _root(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise SystemExit(f"not a directory: {path}")
    return path


def cmd_discover(args: argparse.Namespace) -> int:
    profile = discover(_root(args.path))
    print(profile.to_json())
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    from .discovery.classify import classify

    result = classify(_root(args.path))
    if args.github_output:
        out = os.environ.get("GITHUB_OUTPUT")
        if not out:
            print("GITHUB_OUTPUT is not set", file=sys.stderr)
            return 2
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"targets={json.dumps(result.targets)}\n")
            fh.write(f"kinds={json.dumps(result.kinds)}\n")
            for t in ("web", "android", "ios", "windows"):
                fh.write(f"has_{t}={'true' if t in result.targets else 'false'}\n")
    print(result.to_json())
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from .discovery.classify import classify
    from .planning.generate import build_plan, render_plan

    root = _root(args.path)
    classification = classify(root)
    plan, _ = build_plan(classification, discover(root))
    print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) if args.json else render_plan(plan, classification))
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    from .planning.auto import run_auto

    result = run_auto(_root(args.path), target=args.target, use_ai=not args.no_ai, setup=not args.no_setup,
                      regenerate=args.regenerate, out_dir=Path(args.out).resolve() if args.out else None,
                      publish=args.publish, fail_on=args.fail_on)
    report = result.report
    print(f"\nkinds: {', '.join(result.classification.kinds) or 'none'} · targets: {', '.join(result.classification.targets)}")
    print(f"contract: {result.contract_source} · planned suites: {len(result.plan.suites)} · AI planner: {result.planner.get('status')}")
    print(f"{report['decision']['decision']}: " + "; ".join(report["decision"]["reasons"]))
    print(f"Report: {report['out_dir']}/report.md")
    return exit_code(report, args.fail_on)


def cmd_init(args: argparse.Namespace) -> int:
    """Write the generated contract and plan without running anything."""
    from .discovery.classify import classify
    from .planning.auto import write_contract
    from .planning.generate import build_plan, render_plan

    root = _root(args.path)
    target = root / CONFIG_DIR / CONFIG_FILE
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite).", file=sys.stderr)
        return 2
    classification = classify(root)
    plan, contract = build_plan(classification, discover(root))
    write_contract(root, contract)
    (target.parent / "plan.md").write_text(render_plan(plan, classification), encoding="utf-8")
    (target.parent / "classification.json").write_text(classification.to_json(), encoding="utf-8")
    print(f"Contract written to {target} ({len(plan.suites)} planned suites, kinds: {', '.join(classification.kinds) or 'none'}).")
    print("Next: `pqe auto` (adds AI-designed goal tests and runs everything) or `pqe run`.")
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

    p = sub.add_parser("auto", help="classify, plan, design and run every test the project needs")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--target", choices=["", "web", "android", "ios", "windows"], default="", help="run only one environment (CI fan-out)")
    p.add_argument("--no-ai", action="store_true", help="skip the AI test designer")
    p.add_argument("--no-setup", action="store_true", help="do not install dependencies first")
    p.add_argument("--regenerate", action="store_true", help="regenerate even a human-owned contract (it is not overwritten on disk)")
    p.add_argument("--out", help="output directory")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--fail-on", choices=["no_go", "conditional", "never"], default="no_go")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("classify", help="what kind of software is this?")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--github-output", action="store_true", help="also write targets/kinds to $GITHUB_OUTPUT")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("plan", help="which tests does this project need?")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("discover", help="profile a project (read-only)")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("init", help="write the generated contract and plan")
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
    run_p.add_argument("--fail-on", choices=["no_go", "conditional", "never"], default="no_go")
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
