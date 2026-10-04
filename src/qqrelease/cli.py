"""qqrelease: release's command line.

    qqrelease lkgr --config <infra-config checkout> --state <state dir> [--publish] [--repo NAME]...
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import backends as garden_backends
from qqgarden.errors import GardenerError
from qqgarden.postsubmit import parse_time

from qqrelease import backends, config, executor, lkgr
from qqrelease.errors import ReleaseError
from qqrelease.store import Store

# A commit whose post-submit run has not shown up yet is pending, not missing, for this long. Only
# green counts for lkgr, so this changes wording, never a decision. Same value as the gardener's.
GRACE_MINUTES = 15


def _actor() -> str:
    if os.environ.get("GITHUB_RUN_ID"):
        return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return "local"


def _mirror(args, cfg: dict):
    name = config.backend_name(cfg, args.backend)
    slugs = {r.name: r.slug for r in config.repos(cfg)}
    return backends.load(name, target_root=args.target_root, repos=slugs)


def _source(args, cfg: dict):
    """Where commits and post-submit runs come from: the gardener's backends."""
    if args.snapshot:
        return garden_backends.load("snapshot", path=args.snapshot)
    return garden_backends.load(config.backend_name(cfg, args.backend), cache=args.cache)


def cmd_lkgr(args) -> int:
    cfg = config.load(Path(args.config))
    ref = config.lkgr_ref(cfg)
    repos = config.repos(cfg, args.repo)
    store = Store(args.state, push=args.publish)
    mirror = _mirror(args, cfg)
    source = _source(args, cfg)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    lines = ["# lkgr", "", "| Repo | Action | lkgr | Why | Target ref |", "|---|---|---|---|---|"]
    rc = 0
    for repo in repos:
        try:
            commits = source.commits(repo, args.limit)
            runs = []
            for b in repo.postsubmit:
                r, _note = source.runs(repo, b)
                runs.extend(r)
        except GardenerError as e:
            raise ReleaseError(f"{repo.name}: {e}") from None
        cur = store.pointer(repo.name, ref)
        d = lkgr.decide(repo.postsubmit, commits, runs, cur.commit, now,
                        timedelta(minutes=GRACE_MINUTES))
        mirror_note = ""
        if d.action in ("advance", "retreat") and not args.dry_run:
            op = executor.plan(store, d.action, repo.name, ref, d.commit, reason=d.reason, actor=_actor())
            op, _ = executor.move(store, mirror, op, at=args.now)
            mirror_note = op.mirror
        if d.action == "stuck":
            rc = 1
            print(f"::error::{repo.name}: {d.reason}", file=sys.stderr)
        shown = d.commit[:12] if d.commit else "(none)"
        lines.append(f"| {repo.name} | **{d.action}**{' (dry run)' if args.dry_run and d.action in ('advance', 'retreat') else ''} "
                     f"| {shown} | {d.reason} | {mirror_note} |")
    print("\n".join(lines))
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qqrelease", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s):
        s.add_argument("--config", required=True, help="infra-config checkout at the pins.toml commit")
        s.add_argument("--state", required=True, help="state store: a worktree of the release-state branch")
        s.add_argument("--publish", action="store_true", help="push each state change before acting")
        s.add_argument("--repo", action="append", help="limit to these onboarded repos")
        s.add_argument("--backend", help="override pipelines.toml [defaults] backend (github, local)")
        s.add_argument("--target-root", help="local backend: directory holding the target git repos")
        s.add_argument("--now", help="RFC 3339 time to act at (tests and replays)")

    s = sub.add_parser("lkgr", help="advance lkgr to the newest all-green main commit (V0-REL-01)")
    common(s)
    s.add_argument("--snapshot", help="read commits and runs from a gardener snapshot JSON")
    s.add_argument("--cache", default=".qq/git", help="where the github backend keeps its clones")
    s.add_argument("--limit", type=int, default=100, help="main commits to consider, newest first")
    s.add_argument("--dry-run", action="store_true", help="decide, but move nothing")
    s.set_defaults(func=cmd_lkgr)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as e:
        print(f"qqrelease: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # never let a crash look like exit 1
        print(f"qqrelease: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
