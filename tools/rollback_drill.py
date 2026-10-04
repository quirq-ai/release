"""V0-REL-02 done-when: a rollback drill restores the previous canary in under 10 minutes.

Sets up a local target repo for each onboarded repo that ships on canary, ships two canaries through
the executor exactly as the pipeline does (lkgr moves, canary is promoted to it with an artifact
digest), then runs `qqrelease channel rollback` and checks, for each repo:

- the target repo's `channels/canary` ref names the previous canary's commit again;
- the state store's pointer and the published `channels.json` name its commit and digest again;
- the rollback is a recorded, applied operation;
- the whole rollback took under 10 minutes (the clock starts at the rollback command).

This is the offline half of the done-when: the executor, the store and a real git ref. The live half
(job start plus the GitHub write) is bounded by the channel-rollback workflow's 10-minute timeout.

    python tools/rollback_drill.py --config .qq/infra-config [--budget-seconds 600]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from qqrelease import backends, channels, cli, config, executor
from qqrelease.store import Store

CHANNEL = "canary"


def git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--budget-seconds", type=float, default=600)
    args = ap.parse_args(argv)
    cfg = config.load(Path(args.config))
    ref = config.lkgr_ref(cfg)
    names = [r["name"] for r in cfg["repos"]["repo"] if CHANNEL in r.get("channels", [])]
    if not names:
        print(f"no onboarded repo ships on {CHANNEL}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        state = tmp / "state"
        state.mkdir()
        git("init", "-q", "-b", "release-state", cwd=state)
        store = Store(state)
        mirror = backends.load("local", target_root=tmp / "targets")
        shipped = {}
        for name in names:
            d = tmp / "targets" / name
            d.mkdir(parents=True)
            git("init", "-q", "-b", "main", cwd=d)
            shas = []
            for i in range(2):
                git("-c", "user.name=drill", "-c", "user.email=drill@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", f"commit {i + 1}", cwd=d)
                shas.append(git("rev-parse", "HEAD", cwd=d))
            for sha in shas:   # two daily canaries
                executor.move(store, mirror, executor.plan(store, "advance", name, ref, sha))
                op = channels.plan_promote(cfg, store, name, CHANNEL, sha, digest(f"{name}@{sha}"))
                channels.apply(store, mirror, op)
            shipped[name] = shas

        failures = []
        for name, (prev, bad) in shipped.items():
            start = time.monotonic()
            rc = cli.main(["channel", "rollback", "--config", args.config, "--state", str(state),
                           "--backend", "local", "--target-root", str(tmp / "targets"),
                           "--repo", name, "--channel", CHANNEL, "--reason", "rollback drill"])
            elapsed = time.monotonic() - start
            tip = git("rev-parse", f"refs/heads/channels/{CHANNEL}", cwd=tmp / "targets" / name)
            ptr = store.pointer(name, channels.ref_of(CHANNEL))
            published = json.loads((state / channels.MANIFEST).read_text()).get("repos", {}).get(name, {}).get(
                CHANNEL, {"commit": "", "digest": ""})
            op = store.op(ptr.op)
            checks = {
                "exit 0": rc == 0,
                "ref restored": tip == prev,
                "pointer restored": (ptr.commit, ptr.digest) == (prev, digest(f"{name}@{prev}")),
                "channels.json restored": (published["commit"], published["digest"]) == (ptr.commit, ptr.digest),
                "operation applied": op is not None and op.kind == "rollback" and op.state == "applied",
                f"under {args.budget_seconds:.0f} s": elapsed < args.budget_seconds,
            }
            bad_checks = [k for k, ok in checks.items() if not ok]
            print(f"{name}: canary {bad[:12]} -> {prev[:12]} in {elapsed:.2f} s; "
                  + ("ok" if not bad_checks else "FAILED: " + ", ".join(bad_checks)))
            failures += bad_checks
    if failures:
        return 1
    print(f"ok: the rollback drill restored the previous {CHANNEL} for {len(shipped)} repos, each well "
          f"under {args.budget_seconds / 60:.0f} minutes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
