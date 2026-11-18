"""Command line: ``diamond review``, ``diamond analyze``, ``diamond mcp``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config


def _cmd_review(args: argparse.Namespace) -> int:
    from .pipeline import analyze
    from .recorder import ReviewSession

    repo = Path(args.repo).resolve() if args.repo else None
    cfg = load_config(repo)
    session = ReviewSession(cfg, args.url, repo=repo, audio=not args.no_audio)
    session.start()
    print(f"💎 Diamond is recording → {session.store.root}")
    print(f"   microphone: {session.audio_status}")
    print("   talk while pointing; Alt+Shift+M marks the element under the mouse.")
    print("   close the browser window (or Ctrl+C) to finish.")
    try:
        root = session.wait()
    except KeyboardInterrupt:
        root = session.stop()
    if args.no_analyze:
        print(root)
        return 0
    outcome = analyze(root, cfg, repo)
    print(json.dumps(outcome.summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .pipeline import analyze

    repo = Path(args.repo).resolve() if args.repo else None
    cfg = load_config(repo)
    if args.no_speech:
        cfg.speech.provider = "none"
    outcome = analyze(Path(args.session), cfg, repo)
    print(json.dumps(outcome.summary(), ensure_ascii=False, indent=2))
    return 2 if args.fail_on and any(f.severity.rank <= ["SEV0", "SEV1", "SEV2", "SEV3"].index(args.fail_on)
                                     for f in outcome.findings) else 0


def _cmd_mcp(args: argparse.Namespace) -> int:  # noqa: ARG001
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diamond", description="Narrated design & code review engine")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="open a browser and record a narrated review")
    review.add_argument("url")
    review.add_argument("--repo", help="path of the project's source (enables root-cause tracing)")
    review.add_argument("--no-audio", action="store_true", help="record without the microphone")
    review.add_argument("--no-analyze", action="store_true")
    review.set_defaults(func=_cmd_review)

    an = sub.add_parser("analyze", help="analyze a recorded session directory")
    an.add_argument("session")
    an.add_argument("--repo")
    an.add_argument("--no-speech", action="store_true")
    an.add_argument("--fail-on", choices=["SEV0", "SEV1", "SEV2", "SEV3"], help="exit 2 if a finding at/above this severity exists")
    an.set_defaults(func=_cmd_analyze)

    mcp = sub.add_parser("mcp", help="run the MCP server (stdio)")
    mcp.set_defaults(func=_cmd_mcp)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
