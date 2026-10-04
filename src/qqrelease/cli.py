"""qqrelease: release's command line. `qq channel ...` is the same channel command, added to depot's
`qq` through the `qq.commands` entry point.

    qqrelease lkgr --config <infra-config checkout> --state <state dir> [--publish] [--repo NAME]...
    qqrelease channel promote  --config ... --state ... --repo NAME --channel canary --commit SHA --digest sha256:...
    qqrelease channel rollback --config ... --state ... --repo NAME --channel canary
    qqrelease channel show     --state ...
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import backends as garden_backends
from qqgarden.errors import GardenerError
from qqgarden.postsubmit import parse_time

from qqrelease import backends, canary, channels, config, executor, lkgr, report
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
    if (settled is not None and settled.state == "applied" and settled.kind == kind == "promote"
            and (settled.to_commit, settled.digest) == (args.commit, args.digest)):
        # A retry of a promote whose write landed: the channel already names what was asked.
        print(f"the pending promote {settled.key} landed: {args.repo} {args.channel} names "
              f"{settled.to_commit[:12]} {settled.digest}")
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
        cfg, store, args.repo, args.channel, reason=args.reason, actor=actor,
        from_commit=args.from_commit), "rollback")


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
    s.add_argument("--from", dest="from_commit", required=True, metavar="COMMIT",
                   help="the full commit the channel names now (channels.json); refused if it has moved, so "
                        "a re-run or a second dispatch never rolls back twice")
    s.set_defaults(func=cmd_rollback)
    s = csub.add_parser("show", help="print channels.json: what every channel names")
    s.add_argument("--state", required=True)
    s.set_defaults(func=cmd_show)


def _today(args) -> str:
    return args.date or datetime.now(timezone.utc).date().isoformat()


def cmd_canary_plan(args) -> int:
    """Stage 1 for every canary repo, as JSON: [{repo, slug, action, commit, previous, reason}]."""
    cfg = config.load(Path(args.config))
    store = Store(args.state)
    slugs = {r.name: r.slug for r in config.repos(cfg)}
    plan = [{**dataclasses.asdict(canary.select(cfg, store, name)), "slug": slugs.get(name, "")}
            for name in canary.canary_repos(cfg)]
    text = json.dumps(plan, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


def cmd_canary_stages(args) -> int:
    """Stages 2-6 on a worker; writes <out>/stages.json whatever happens."""
    cfg = config.load(Path(args.config))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = canary.run_stages(cfg, args.repo, args.commit, Path(args.src), out, args.toolchain, _today(args))
    (out / "stages.json").write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n")
    for st in doc["stages"]:
        first = st["detail"].splitlines()[0] if st["detail"] else ""
        print(f"{'PASS' if st['ok'] else 'FAIL'} {st['name']} ({st['seconds']} s) {first}")
    return 0


def cmd_canary_finish(args) -> int:
    """Stage 7 and the day's records, under the executor's lock."""
    cfg = config.load(Path(args.config))
    store = Store(args.state, push=args.publish)
    mirror = _mirror(args, cfg)
    plan = json.loads(Path(args.plan).read_text())
    date = _today(args)
    held, rc = [], 0
    lines = [f"# Canary {date}", "", "| Repo | Outcome | Commit | Digest | Why |", "|---|---|---|---|---|"]
    try:
        for item in plan:
            sel = canary.Selection(**{k: item[k] for k in ("repo", "action", "commit", "previous", "reason")})
            stages = None
            f = Path(args.stages_dir) / sel.repo / "stages.json"
            try:        # written where product code runs: any shape at all is "no results" (S5 counts it)
                stages = json.loads(f.read_text()) if f.is_file() else None
            except (OSError, ValueError):
                stages = None
            try:
                run = canary.finish(cfg, store, mirror, sel, stages, date, run_url=args.run_url, at=args.now)
            except Exception as e:      # one repo's failure never loses another repo's record or hold
                rc = 2
                print(f"::error::{sel.repo}: {type(e).__name__}: {e}", file=sys.stderr)
                lines.append(f"| {sel.repo} | **error** | | | {' '.join(str(e).replace('|', '/').split())} |")
                continue
            if run.outcome == "error":
                rc = 2                      # the job goes red; the watchdog runs the day again
                print(f"::error::{sel.repo}: {run.reason}", file=sys.stderr)
            if run.outcome == "held":
                held.append({"repo": run.repo, "commit": run.commit, "previous": run.previous,
                             "digest": run.digest,
                             "stage": next((s["name"] for s in run.stages if s.get("ok") is False), "pipeline"),
                             "summary": f"Canary held for {run.repo}: {run.reason}"[:200]})
            why = " ".join(run.reason.replace("|", "/").split())
            lines.append(f"| {run.repo} | **{run.outcome}** | {run.commit[:12]} | {run.digest[:19]} | {why} |")
    finally:
        if args.held_out:
            Path(args.held_out).write_text(json.dumps(held, sort_keys=True) + "\n")
    print("\n".join(lines))
    return rc


def cmd_canary_missing(args) -> int:
    cfg = config.load(Path(args.config))
    print(",".join(canary.missing_runs(cfg, Store(args.state), _today(args))))
    return 0


def cmd_canary_postmortem(args) -> int:
    store = Store(args.state)
    found = [r for r in canary.runs_on(store, args.repo, _today(args))
             if r.get("outcome") == "held" and (not args.commit or r.get("commit") == args.commit)]
    if not found:
        raise ReleaseError(f"no held canary run for {args.repo} {args.commit[:12]} on {_today(args)}")
    run = canary.CanaryRun.from_dict(found[0])
    print(canary.postmortem_draft(Path(args.config), run, args.failure_issue), end="")
    return 0


def cmd_canary_release_hold(args) -> int:
    """Release a held canary commit (a hold the machine caused): the next canary builds it again."""
    store = Store(args.state, push=args.publish)
    if args.repo not in canary.canary_repos(config.load(Path(args.config))):
        raise ReleaseError(f"{args.repo!r} is not a canary repo in infra-config")
    outcome, key = canary.release_hold(store, args.repo, args.commit, args.reason, args.released_before,
                                       actor=args.actor, requested_by=args.requested_by)
    print(f"{outcome}: {args.repo} {args.commit[:12]} (operation {key[:12] or 'unknown'}); "
          "the next canary run builds it again")
    return 0


def cmd_canary_toolchains(args) -> int:
    """`name=version` lines for the toolchains a checkout's manifest pins (for a workflow's outputs)."""
    if not (Path(args.src) / canary.MANIFEST).is_file():
        return 0          # not onboarded: the build stage says so
    for name, version in canary.toolchain_versions(Path(args.src)).items():
        # Product-controlled text going into $GITHUB_OUTPUT: one plain token per line, or nothing.
        if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
            print(f"{name}={version}")
    return 0


def cmd_canary_report(args) -> int:
    """The day's report (V0-REL-04): writes reports/<date>.md on release-state and prints it."""
    cfg = config.load(Path(args.config))
    store = Store(args.state, push=args.publish)
    records = None
    if args.open_records:
        try:
            records = json.loads(Path(args.open_records).read_text())
        except (OSError, ValueError):
            records = None
    path = report.write(cfg, store, _today(args), records)
    print(path.read_text(), end="")
    return 0


def add_canary(sub) -> None:
    c = sub.add_parser("canary", help="the daily canary pipeline (V0-REL-03)")
    csub = c.add_subparsers(dest="canary_cmd", required=True)
    s = csub.add_parser("plan", help="stage 1 (select) for every canary repo, as JSON")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--out", help="also write the JSON here")
    s.set_defaults(func=cmd_canary_plan)
    s = csub.add_parser("stages", help="stages 2-6 (build, verify, fuzz smoke, deploy and probe)")
    s.add_argument("--config", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--src", required=True, help="a checkout of --commit")
    s.add_argument("--out", required=True, help="results directory; stages.json goes here")
    s.add_argument("--toolchain", action="append", default=[], metavar="NAME=ROOT")
    s.add_argument("--date")
    s.set_defaults(func=cmd_canary_stages)
    s = csub.add_parser("finish", help="stage 7 (promote) and the day's records")
    _common(s)
    s.add_argument("--plan", required=True, help="the JSON `canary plan` wrote")
    s.add_argument("--stages-dir", required=True, help="holds <repo>/stages.json from each worker")
    s.add_argument("--held-out", help="write the held canaries here, as JSON")
    s.add_argument("--run-url", default="")
    s.add_argument("--date")
    s.set_defaults(func=cmd_canary_finish)
    s = csub.add_parser("release-hold", help="release a held canary commit so the next canary builds it")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--publish", action="store_true", help="push release-state")
    s.add_argument("--repo", required=True)
    s.add_argument("--commit", required=True, metavar="COMMIT", help="the full held commit")
    s.add_argument("--reason", required=True, help="why (recorded in the operation and the hold record)")
    s.add_argument("--released-before", type=int, required=True, metavar="N",
                   help="how many times this commit was released before this hold (the hold record's "
                        "`releases`; 0 for a first hold): names the hold, so an old dispatch is refused")
    s.add_argument("--actor", default="local", help="where it ran: a workflow run URL, or local")
    s.add_argument("--requested-by", default="local", help="who asked: the dispatching account(s)")
    s.set_defaults(func=cmd_canary_release_hold)
    s = csub.add_parser("toolchains", help="name=version for each toolchain the manifest pins")
    s.add_argument("--src", required=True)
    s.set_defaults(func=cmd_canary_toolchains)
    s = csub.add_parser("report", help="the daily canary report (V0-REL-04)")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--publish", action="store_true")
    s.add_argument("--date")
    s.add_argument("--open-records", help="JSON [{number, title, url}] of open failure issues")
    s.set_defaults(func=cmd_canary_report)
    s = csub.add_parser("missing", help="canary repos with no run record for the day (the watchdog)")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--date")
    s.set_defaults(func=cmd_canary_missing)
    s = csub.add_parser("postmortem", help="a postmortem draft for the day's held canary")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--date")
    s.add_argument("--commit", default="", help="the held commit (the day may hold one and ship another)")
    s.add_argument("--failure-issue", default="")
    s.set_defaults(func=cmd_canary_postmortem)


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
    add_canary(sub)

    args = p.parse_args(argv)
    return guarded(args.func, args)


if __name__ == "__main__":
    sys.exit(main())
