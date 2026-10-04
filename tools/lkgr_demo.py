"""V0-REL-01 done-when, offline: `lkgr` moves on its own and never points at a red commit.

Builds one local git repo per onboarded repo in infra-config, then replays a main history that
grows one commit per tick, with post-submit verdicts arriving late, some red, and one green that a
re-run turns red. Each tick runs `qqrelease lkgr` exactly as the workflow does (the local backend in
place of GitHub) and checks:

- lkgr moved with no input but the verdicts;
- the target repo's `lkgr` ref and the state store agree;
- lkgr names only a commit that is green on every builder at that tick.

    tools/lkgr_demo.py --config .qq/infra-config
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqrelease import cli, config

# One tick per letter. Letter i is the verdict commit i gets one tick after it lands (it is pending
# on its own tick). G: green, then a re-run at the next tick turns it red.
TIMELINE = "ggrgGrggrrg"
START = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)
TICK = timedelta(minutes=20)


def git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(d: Path, n: int) -> list[str]:
    d.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=d)
    shas = []
    for i in range(n):
        when = (START + i * TICK).isoformat()
        subprocess.run(["git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "commit",
                        "-q", "--allow-empty", "-m", f"commit {i + 1}"], cwd=d, check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
        shas.append(git("rev-parse", "HEAD", cwd=d))
    return shas


def runs_at(tick: int, builder: str, shas: list[str]) -> list[dict]:
    """Runs visible at `tick` (commit `tick` has just landed)."""
    out = []
    for i in range(tick + 1):
        rid = 1000 + i
        if i == tick:
            out.append(dict(builder=builder, commit=shas[i], status="in_progress", id=str(rid)))
            continue
        ch = TIMELINE[i]
        first = "failure" if ch == "r" else "success"
        out.append(dict(builder=builder, commit=shas[i], status="completed", conclusion=first, id=str(rid)))
        if ch == "G" and tick >= i + 2:
            out.append(dict(builder=builder, commit=shas[i], status="completed", conclusion="failure",
                            id=str(rid), attempt=2))
    return out


def green_now(tick: int, i: int) -> bool:
    if i >= tick:
        return False
    ch = TIMELINE[i]
    return ch == "g" or (ch == "G" and tick < i + 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    cfg = config.load(Path(args.config))
    repos = [r for r in config.repos(cfg) if r.postsubmit]
    if not repos:
        print("no onboarded repo has a post-submit builder", file=sys.stderr)
        return 1
    ref = config.lkgr_ref(cfg)
    n = len(TIMELINE)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shas = {r.name: make_repo(tmp / "targets" / r.name, n) for r in repos}
        state = tmp / "state"
        state.mkdir()
        git("init", "-q", "-b", "release-state", cwd=state)
        moves, retreats, last = 0, 0, {r.name: "" for r in repos}
        for tick in range(n):
            snap = {"repos": {}}
            for r in repos:
                commits = [dict(sha=s, landed_at=(START + i * TICK).isoformat(), title=f"commit {i + 1}")
                           for i, s in enumerate(shas[r.name][:tick + 1])][::-1]
                runs = [x for b in r.postsubmit for x in runs_at(tick, b, shas[r.name])]
                snap["repos"][r.name] = {"commits": commits, "runs": runs}
            (tmp / "snap.json").write_text(json.dumps(snap))
            now = (START + tick * TICK + timedelta(minutes=5)).isoformat()
            rc = cli.main(["lkgr", "--config", args.config, "--state", str(state), "--backend", "local",
                           "--target-root", str(tmp / "targets"), "--snapshot", str(tmp / "snap.json"),
                           "--now", now])
            if rc != 0:
                print(f"tick {tick}: qqrelease lkgr exited {rc}", file=sys.stderr)
                return 1
            for r in repos:
                ptr = json.loads((state / "pointers" / r.name / f"{ref}.json").read_text()) \
                    if (state / "pointers" / r.name / f"{ref}.json").is_file() else {"commit": ""}
                tip = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}"],
                                     cwd=tmp / "targets" / r.name, capture_output=True, text=True).stdout.strip()
                if tip != ptr["commit"]:
                    print(f"tick {tick}: {r.name} ref {tip[:12]} != state {ptr['commit'][:12]}", file=sys.stderr)
                    return 1
                if tip:
                    i = shas[r.name].index(tip)
                    if not green_now(tick, i):
                        print(f"FAIL tick {tick}: {r.name} lkgr names commit {i + 1}, which is not green",
                              file=sys.stderr)
                        return 1
                if tip != last[r.name]:
                    moves += 1
                    if last[r.name] and shas[r.name].index(tip) < shas[r.name].index(last[r.name]):
                        retreats += 1
                    last[r.name] = tip
        ops = len(list((state / "ops").glob("*.json")))
        log = git("log", "--oneline", cwd=state).count("\n") + 1
    if moves < 2 * len(repos) or retreats < len(repos):
        print(f"FAIL: lkgr moved {moves} times ({retreats} back after a re-run turned it red)", file=sys.stderr)
        return 1
    print(f"ok: lkgr moved {moves} times across {len(repos)} repos over {n} ticks with no input but "
          f"verdicts, never onto a red commit ({retreats} moved back when a re-run turned lkgr red); {ops} operations recorded, {log} state commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
