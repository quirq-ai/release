import json
import re
import subprocess
from pathlib import Path

import pytest

from qqrelease import canary, channels, executor
from qqrelease.backends import load
from qqrelease.store import Store

ROOT = Path(__file__).resolve().parent.parent


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, cfg):
    repo = canary.canary_repos(cfg)[0]
    d = tmp_path / "targets" / repo
    d.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=d)
    shas = []
    for i in range(3):
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty",
            "-m", f"c{i}", cwd=d)
        shas.append(git("rev-parse", "HEAD", cwd=d))
    state = tmp_path / "state"
    state.mkdir()
    git("init", "-q", "-b", "release-state", cwd=state)
    return cfg, Store(state), load("local", target_root=tmp_path / "targets"), repo, shas


def lkgr_to(store, mirror, repo, sha):
    executor.move(store, mirror, executor.plan(store, "advance", repo, "lkgr", sha))


def passed(repo, commit, digest="sha256:" + "a" * 64):
    return {"repo": repo, "commit": commit, "digest": digest, "ok": True,
            "stages": [{"name": n, "ok": True, "detail": "", "seconds": 1} for n in
                       ("build", "verify", "fuzz-smoke", "deploy-probe")]}


def test_canary_repos_come_from_pipelines_and_repos(cfg):
    names = canary.canary_repos(cfg)
    assert names
    for n in names:
        assert any(b.get("pipeline") == "release" and b.get("channel") == "canary" and b["repo"] == n
                   for b in cfg["pipelines"]["builder"])


def test_the_workflow_runs_on_the_schedule_in_channels_toml(cfg):
    want = next(c for c in cfg["channels"]["channel"] if c["name"] == "canary")["schedule"]
    text = (ROOT / ".github" / "workflows" / "canary.yml").read_text()
    assert re.findall(r'cron: "([^"]+)"', text) == [want]


def test_select(world):
    cfg, store, mirror, repo, shas = world
    assert canary.select(cfg, store, repo).action == "noop"                  # no lkgr yet
    lkgr_to(store, mirror, repo, shas[1])
    sel = canary.select(cfg, store, repo)
    assert (sel.action, sel.commit) == ("build", shas[1])
    canary.finish(cfg, store, mirror, sel, passed(repo, shas[1]), "2026-10-05")
    assert canary.select(cfg, store, repo).action == "noop"                  # lkgr has not moved
    lkgr_to(store, mirror, repo, shas[2])
    held = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-06")
    assert held.outcome == "held"
    again = canary.select(cfg, store, repo)
    assert again.action == "noop" and "held" in again.reason                 # never retried


def test_ship_promotes_and_records(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    assert run.outcome == "shipped" and run.operation
    ptr = store.pointer(repo, "channels/canary")
    assert (ptr.commit, ptr.digest) == (shas[1], "sha256:" + "a" * 64)
    rec = json.loads(canary.run_path(store, repo, "2026-10-05").read_text())
    assert rec["schema"] == "qq-canary-run/1" and rec["stages"][-1]["name"] == "promote"


def test_a_commit_canary_was_rolled_back_from_is_not_rebuilt(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    lkgr_to(store, mirror, repo, shas[2])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[2]), "2026-10-06")
    channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary", "bad in use"))
    assert store.pointer(repo, "channels/canary").commit == shas[1]
    sel = canary.select(cfg, store, repo)                                    # lkgr still names shas[2]
    assert sel.action == "noop" and "rolled back" in sel.reason


def test_a_failed_stage_holds_and_keeps_the_previous_canary(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    lkgr_to(store, mirror, repo, shas[2])
    bad = passed(repo, shas[2])
    bad["ok"] = False
    bad["stages"][3] = {"name": "deploy-probe", "ok": False, "detail": "probes failed: /health", "seconds": 1}
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-06")
    assert run.outcome == "held" and "deploy-probe" in run.reason
    assert store.pointer(repo, "channels/canary").commit == shas[1]
    assert canary.held_path(store, repo, shas[2]).is_file()
    draft = canary.postmortem_draft(Path("."), run, "https://example.invalid/issues/1")
    assert "canary-deploy-failed" in draft and "probes failed: /health" in draft and shas[1][:12] in draft


def test_stage_results_for_another_commit_are_a_missing_signal(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[0]), "2026-10-05")
    assert run.outcome == "held" and "missing signal" in run.reason


def test_a_second_run_on_a_day_keeps_the_first(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-05")
    rec = json.loads(canary.run_path(store, repo, "2026-10-05").read_text())
    assert rec["outcome"] == "noop" and rec["earlier"][0]["outcome"] == "shipped"


def test_missing_runs_is_what_the_watchdog_starts(world):
    cfg, store, mirror, repo, shas = world
    assert repo in canary.missing_runs(cfg, store, "2026-10-05")
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-05")
    assert repo not in canary.missing_runs(cfg, store, "2026-10-05")


def test_probe_checks():
    required = [{"name": "h", "path": "/health"}]
    dep = lambda probes, ready=True: [{"deployment": {"target": "s", "ready": ready, "probes": probes}}]
    assert canary.check_probes(dep([{"path": "/health", "ok": True}]), required)[0]
    assert not canary.check_probes(dep([{"path": "/health", "ok": False}]), required)[0]
    ok, why = canary.check_probes(dep([{"path": "/", "ok": True}]), required)
    assert not ok and "did not run" in why
    assert not canary.check_probes(dep([], ready=False), [])[0]
    ok, why = canary.check_probes([], [])
    assert not ok and "missing signal" in why


def test_artifact_digest_is_stable_and_needs_a_build():
    recs = [{"capability": "build", "target": "s", "name": "compile", "digest": "sha256:1",
             "output_digests": {"b": "x", "a": "y"}},
            {"capability": "fetch", "target": "s", "name": "x", "digest": "sha256:2"}]
    d = canary.artifact_digest(recs)
    assert d.startswith("sha256:") and d == canary.artifact_digest(list(reversed(recs)))
    assert canary.artifact_digest([recs[1]]) == ""


def test_a_repo_without_a_manifest_is_a_noop_not_a_held_canary(world, tmp_path):
    cfg, store, mirror, repo, shas = world
    doc = canary.run_stages(cfg, repo, shas[1], tmp_path / "empty", tmp_path / "out", [], "2026-10-05")
    assert doc["skip"] and not doc["stages"]
    lkgr_to(store, mirror, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), doc, "2026-10-05")
    assert run.outcome == "noop" and "not onboarded" in run.reason
    assert not canary.held_path(store, repo, shas[1]).exists()
