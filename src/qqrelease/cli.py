"""qqrelease: release's command line. `qq channel ...` is the same channel command, added to depot's
`qq` through the `qq.commands` entry point.

    qqrelease lkgr --config <infra-config checkout> --state <state dir> [--publish] [--repo NAME]...
    qqrelease channel promote  --config ... --state ... --repo NAME --channel canary --commit SHA --digest sha256:...
    qqrelease channel rollback --config ... --state ... --repo NAME --channel canary
    qqrelease channel show     --state ...
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import backends as garden_backends
from qqgarden.errors import GardenerError
from qqgarden.postsubmit import parse_time

from qqrelease import backends, channels, config, executor, lkgr
from qqrelease.errors import ReleaseError
from qqrelease.store import Store

# A commit whose post-submit run has not shown up yet is pending, not missing, for this long. Only
# green counts for lkgr, so this changes wording, never a decision. Same value as the gardener's.
GRACE_MINUTES = 15


def _mirror(args, cfg: dict):
    name = config.backend_name(cfg, args.backend)
    slugs = {r.name: r.slug for r in config.repos(cfg)}
    return backends.load(name, target_root=args.target_root, repos=slugs)


def _source(args, cfg: dict):
    """Where commits and post-submit runs come from: the gardener's backends."""
    if args.snapshot:
        return garden_backends.load("snapshot", path=args.snapshot)
    return garden_backends.load(config.backend_name(cfg, args.backend), cache=args.cache)


def _lkgr_one(args, store, mirror, source, repo, ref, now) -> str:
    commits = source.commits(repo, args.limit)
    runs = []
    for b in repo.postsubmit:
        r, _note = source.runs(repo, b)
        runs.extend(r)
    if not args.dry_run:
        executor.finish_pending(store, mirror, repo.name, ref, at=args.now)
    cur = store.pointer(repo.name, ref)
    d = lkgr.decide(repo.postsubmit, commits, runs, cur.commit, now, timedelta(minutes=GRACE_MINUTES))
    mirror_note = ""
    moving = d.action in ("advance", "retreat")
    if moving and not args.dry_run:
        op = executor.plan(store, d.action, repo.name, ref, d.commit, reason=d.reason, actor=mirror.actor())
        op, _ = executor.move(store, mirror, op, at=args.now)
        mirror_note = op.mirror
    if d.action == "stuck":
        print(f"::error::{repo.name}: {d.reason}", file=sys.stderr)
    shown = d.commit[:12] if d.commit else "(none)"
    dry = " (dry run)" if args.dry_run and moving else ""
    return f"| {repo.name} | **{d.action}**{dry} | {shown} | {d.reason} | {mirror_note} |"


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
            row = _lkgr_one(args, store, mirror, source, repo, ref, now)
        except (ReleaseError, GardenerError) as e:
            # One repo's trouble must not stop lkgr for the others.
            rc = 2
            print(f"::error::{repo.name}: {e}", file=sys.stderr)
            why = " ".join(str(e).replace("|", "/").split())
            row = f"| {repo.name} | **error** | | {why} | |"
        if "**stuck**" in row:
            rc = max(rc, 1)
        lines.append(row)
    print("\n".join(lines))
    return rc


def cmd_repos(args) -> int:
    """Onboarded repos' names on the backend's host, comma-separated (for scoping a token)."""
    cfg = config.load(Path(args.config))
    print(",".join(r.slug.rpartition("/")[2] for r in config.repos(cfg) if r.slug))
    return 0


def _channel_move(args, plan, kind: str) -> int:
    cfg = config.load(Path(args.config))
    store = Store(args.state, push=args.publish)
    mirror = _mirror(args, cfg)
    settled = channels.settle(store, mirror, args.repo, args.channel, at=args.now)
    if settled is not None and settled.state == "applied" and settled.kind == kind == "rollback":
        # A retry of a rollback whose write landed: done, never a second rollback.
        print(f"the pending rollback {settled.key} landed: {args.repo} {args.channel} names "
              f"{settled.to_commit[:12]} {settled.digest}; not rolling back again")
        return 0
    op = plan(cfg, store, mirror.actor())
    op, ptr = channels.apply(store, mirror, op, at=args.now)
    print(f"{op.kind} {op.repo} {op.ref}: {op.from_commit[:12] or '(new)'} -> {ptr.commit[:12]} "
          f"{ptr.digest} (operation {op.key}, generation {ptr.generation}; target ref: {op.mirror})")
    return 0


def cmd_promote(args) -> int:
    return _channel_move(args, lambda cfg, store, actor: channels.plan_promote(
        cfg, store, args.repo, args.channel, args.commit, args.digest, reason=args.reason, actor=actor),
        "promote")


def cmd_rollback(args) -> int:
    return _channel_move(args, lambda cfg, store, actor: channels.plan_rollback(
        cfg, store, args.repo, args.channel, reason=args.reason, actor=actor), "rollback")


def cmd_show(args) -> int:
    if not Path(args.state).is_dir():
        raise ReleaseError(f"{args.state}: no state store here")
    print(json.dumps(channels.manifest(Store(args.state)), sort_keys=True, indent=2))
    return 0


def _common(s, many_repos: bool = True):
    s.add_argument("--config", required=True, help="infra-config checkout at the pins.toml commit")
    s.add_argument("--state", required=True, help="state store: a worktree of the release-state branch")
    s.add_argument("--publish", action="store_true", help="push each state change before acting")
    if many_repos:
        s.add_argument("--repo", action="append", help="limit to these onboarded repos")
    else:
        s.add_argument("--repo", required=True, help="the onboarded repo (infra-config repos.toml name)")
    s.add_argument("--backend", help="override pipelines.toml [defaults] backend (github, local)")
    s.add_argument("--target-root", help="local backend: directory holding the target git repos")
    s.add_argument("--now", help="RFC 3339 time to act at (tests and replays)")


def add_channel(sub) -> None:
    """`channel promote|rollback|show`, for qqrelease and for qq."""
    ch = sub.add_parser("channel", help="move or show channel pointers (V0-REL-02)")
    csub = ch.add_subparsers(dest="channel_cmd", required=True)
    s = csub.add_parser("promote", help="point a channel at its source's commit and an artifact digest")
    _common(s, many_repos=False)
    s.add_argument("--channel", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--digest", required=True, help="sha256:<hex> of the artifact built from --commit")
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_promote)
    s = csub.add_parser("rollback", help="point a channel back at its previous commit and digest")
    _common(s, many_repos=False)
    s.add_argument("--channel", required=True)
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_rollback)
    s = csub.add_parser("show", help="print channels.json: what every channel names")
    s.add_argument("--state", required=True)
    s.set_defaults(func=cmd_show)


def guarded(func, args) -> int:
    try:
        return func(args)
    except ReleaseError as e:
        print(f"qqrelease: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # never let a crash look like exit 1
        print(f"qqrelease: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def register(sub) -> None:
    """depot's `qq.commands` entry point: adds `qq channel`."""
    add_channel(sub)
    for name in ("channel",):
        parser = sub.choices[name]
        parser.set_defaults(run=lambda args: guarded(args.func, args))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qqrelease", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("lkgr", help="advance lkgr to the newest all-green main commit (V0-REL-01)")
    _common(s)
    s.add_argument("--snapshot", help="read commits and runs from a gardener snapshot JSON")
    s.add_argument("--cache", default=".qq/git", help="where the github backend keeps its clones")
    s.add_argument("--limit", type=int, default=100, help="main commits to consider, newest first")
    s.add_argument("--dry-run", action="store_true", help="decide, but move nothing")
    s.set_defaults(func=cmd_lkgr)

    s = sub.add_parser("repos", help="onboarded repo names, comma-separated")
    s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_repos)
    add_channel(sub)

    args = p.parse_args(argv)
    return guarded(args.func, args)


if __name__ == "__main__":
    sys.exit(main())
