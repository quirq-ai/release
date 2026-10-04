"""V0-REL-03 done-when, offline: 7 daily canaries in a row with no human touch, and a planted bad
canary is held.

Builds a git repo from `tests/fixtures/canary_app` (a small python-service with a property test) under
the name of the first onboarded canary repo, then plays eight days. Each day one commit lands, lkgr
moves to it through the executor, and the pipeline runs exactly as the `canary` workflow does:
`qqrelease canary plan`, then `canary stages` on a checkout of the selected commit, then
`canary finish` (the local backend in place of GitHub). On day 4 the commit is a planted bad
canary: its /health answers 500, which a health.toml probe requires. Checks:

- days 1-3 and 5-8 ship on their own: `channels/canary` names the day's commit and a digest;
- day 4 is held at deploy-probe: the canary still names day 3, and the held commit is recorded;
- a ninth run with lkgr unmoved records a no-op instead of rebuilding;
- every day has a run record (what the daily report reads).

    tests/canary_demo.py --config .qq/infra-config --toolchain python=ROOT
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from qqrelease import backends, canary, cli, config, executor
from qqrelease.store import Store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "canary_app"
DAYS = 8
BAD_DAY = 4
START = date(2026, 10, 5)


def git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_all(d: Path, msg: str) -> str:
    git("add", "-A", cwd=d)
    git("-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "commit", "-q", "-m", msg, cwd=d)
    return git("rev-parse", "HEAD", cwd=d)


def python_version(toolchains: list[str]) -> str:
    root = dict(t.split("=", 1) for t in toolchains).get("python")
    exe = str(Path(root) / "bin" / "python3") if root else sys.executable
    return subprocess.run([exe, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                          check=True, capture_output=True, text=True).stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--toolchain", action="append", default=[], metavar="NAME=ROOT")
    args = ap.parse_args(argv)
    cfg = config.load(Path(args.config))
    repos = canary.canary_repos(cfg)
    if not repos:
        print("no onboarded repo has a canary release builder", file=sys.stderr)
        return 1
    repo = next((r for r in repos if any(p.get("path") == "/health" for p in canary.required_probes(cfg, r))),
                repos[0])
    ref = config.lkgr_ref(cfg)
    tc = [a for t in args.toolchain for a in ("--toolchain", t)]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "targets" / repo
        shutil.copytree(FIXTURE, src)
        tmpl = src / "infra" / "repo.toml.in"
        (src / "infra" / "repo.toml").write_text(
            tmpl.read_text().replace("@PYTHON_VERSION@", python_version(args.toolchain)))
        tmpl.unlink()
        git("init", "-q", "-b", "main", cwd=src)
        commit_all(src, "fixture")
        state = tmp / "state"
        state.mkdir()
        git("init", "-q", "-b", "release-state", cwd=state)
        store = Store(state)
        mirror = backends.load("local", target_root=tmp / "targets")
        common = ["--config", args.config, "--state", str(state)]
        shipped = []
        for day in range(1, DAYS + 2):
            d = (START + timedelta(days=day - 1)).isoformat()
            if day <= DAYS:
                server = (src / "server.py").read_text()
                healthy = "HEALTHY = True" if day != BAD_DAY else "HEALTHY = False"
                server = server.replace("HEALTHY = False", "HEALTHY = True").replace("HEALTHY = True", healthy)
                (src / "server.py").write_text(server)
                (src / "CHANGELOG").write_text(f"day {day}\n")
                sha = commit_all(src, f"day {day}" + (" (planted bad canary)" if day == BAD_DAY else ""))
                executor.move(store, mirror, executor.plan(store, "advance", repo, ref, sha))
            else:
                d = (START + timedelta(days=DAYS - 1)).isoformat()     # a second run on the last day
            work = tmp / "work" / d
            work.mkdir(parents=True, exist_ok=True)
            assert cli.main(["canary", "plan", *common, "--out", str(work / "plan.json")]) == 0
            plan = json.loads((work / "plan.json").read_text())
            for item in plan:
                if item["action"] != "build":
                    continue
                checkout = work / "src" / item["repo"]
                git("worktree", "add", "-q", "--detach", str(checkout), item["commit"], cwd=src)
                rc = cli.main(["canary", "stages", "--config", args.config, "--repo", item["repo"],
                               "--commit", item["commit"], "--src", str(checkout),
                               "--out", str(work / "stages" / item["repo"]), "--date", d, *tc])
                if rc != 0:
                    failures.append(f"{d}: canary stages exited {rc}")
            rc = cli.main(["canary", "finish", *common, "--backend", "local", "--target-root", str(tmp / "targets"),
                           "--repo", repo, "--plan", str(work / "plan.json"), "--stages-dir", str(work / "stages"),
                           "--held-out", str(work / "held.json"), "--date", d])
            if rc != 0:
                failures.append(f"{d}: canary finish exited {rc}")
            canary_now = store.pointer(repo, "channels/canary")
            run = json.loads(canary.run_path(store, repo, d).read_text())
            head = git("rev-parse", "HEAD", cwd=src)
            if day == BAD_DAY:
                ok = (run["outcome"] == "held" and canary_now.commit == shipped[-1]
                      and canary.held_path(store, repo, head).is_file()
                      and any(s["name"] == "deploy-probe" and not s["ok"] for s in run["stages"]))
                print(f"day {day}: {'ok' if ok else 'FAILED'}: planted bad canary {run['outcome']} ({run['reason']})")
            elif day <= DAYS:
                ok = (run["outcome"] == "shipped" and canary_now.commit == head and canary_now.digest
                      and git("rev-parse", "refs/heads/channels/canary", cwd=src) == head)
                shipped.append(head)
                print(f"day {day}: {'ok' if ok else 'FAILED'}: {run['outcome']} {head[:12]} {canary_now.digest[:19]}")
            else:
                ok = run["outcome"] == "noop" and len(run.get("earlier", [])) == 1
                print(f"rerun: {'ok' if ok else 'FAILED'}: {run['outcome']} ({run['reason']})")
            if not ok:
                failures.append(f"day {day}: unexpected {run['outcome']}: {run['reason']}")
        runs = sorted(p.name for p in (state / "canary" / repo / "runs").glob("*.json"))
        if len(runs) != DAYS:
            failures.append(f"{len(runs)} run records for {DAYS} days")
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"ok: {DAYS - 1} daily canaries shipped with no human touch, the planted bad canary on day "
          f"{BAD_DAY} was held at deploy-probe with the previous canary kept, and a rerun was a no-op")
    return 0


if __name__ == "__main__":
    sys.exit(main())
