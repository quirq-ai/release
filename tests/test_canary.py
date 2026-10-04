import json
import re
import subprocess
from pathlib import Path

import pytest

from qqrelease import canary, channels, executor
from qqrelease.backends import load
from qqrelease.store import Store
from qqrelease.errors import ReleaseError

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
    bad = passed(repo, shas[2])
    bad["stages"][1]["ok"] = False
    held = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-06")
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


def test_the_postmortem_draft_keeps_every_template_section(config_root):
    run = canary.CanaryRun(repo="r", date="2026-10-05", outcome="held", commit="b" * 40, previous="a" * 40,
                           reason="held at verify", stages=[{"name": "verify", "ok": False, "detail": "x",
                                                            "seconds": 1}])
    draft = canary.postmortem_draft(config_root, run)
    tmpl = (config_root / canary.POSTMORTEM_TEMPLATE).read_text().splitlines()
    want = [l for l in tmpl if l.startswith("## ")]
    got = [l for l in draft.splitlines() if l in want]
    assert want and got == want                                       # all of them, in the template's order


def test_stage_results_for_another_commit_are_a_missing_signal(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[0]), "2026-10-05")
    assert run.outcome == "error" and not canary.held_path(store, repo, shas[1]).is_file()
    assert repo in canary.missing_runs(cfg, store, "2026-10-05")          # the watchdog runs it again
    assert canary.select(cfg, store, repo).action == "build"


def test_a_second_run_on_a_day_keeps_the_first(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-05")
    rec = json.loads(canary.run_path(store, repo, "2026-10-05").read_text())
    assert rec["outcome"] == "shipped" and rec["later"][0]["outcome"] == "noop"


def test_a_hold_after_a_ship_on_the_same_day_is_on_top(world, config_root, capsys):
    from qqrelease import cli
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    lkgr_to(store, mirror, repo, shas[2])
    bad = passed(repo, shas[2])
    bad["stages"][1]["ok"] = False
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-05")   # a manual dispatch
    rec = json.loads(canary.run_path(store, repo, "2026-10-05").read_text())
    assert rec["outcome"] == "held" and rec["earlier"][0]["outcome"] == "shipped"
    rc = cli.main(["canary", "postmortem", "--config", str(config_root), "--state", str(store.root),
                   "--repo", repo, "--date", "2026-10-05", "--commit", shas[2]])
    out = capsys.readouterr().out
    assert rc == 0 and "at verify" in out and shas[2][:12] in out


def test_rerunning_finish_after_a_ship_stays_shipped(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    sel = canary.select(cfg, store, repo)
    canary.finish(cfg, store, mirror, sel, passed(repo, shas[1]), "2026-10-05")
    again = canary.finish(cfg, store, mirror, sel, passed(repo, shas[1]), "2026-10-05")   # same plan.json
    assert again.outcome == "shipped" and "a rerun" in again.stages[-1]["detail"]
    assert not canary.held_path(store, repo, shas[1]).is_file()


def test_an_infrastructure_failure_holds_nothing(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    sel = canary.select(cfg, store, repo)

    class Down:
        def actor(self):
            return mirror.actor()

        def can_write(self):
            return True

        def read_ref(self, *a):
            return mirror.read_ref(*a)

        def write_ref(self, *a):
            raise ReleaseError("HTTP 502")

    run = canary.finish(cfg, store, Down(), sel, passed(repo, shas[1]), "2026-10-05")
    assert run.outcome == "error" and "HTTP 502" in run.reason
    assert not canary.held_path(store, repo, shas[1]).is_file()
    assert repo in canary.missing_runs(cfg, store, "2026-10-05")
    # The watchdog's second run: the pending write is settled and the canary ships.
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05")
    assert run.outcome == "shipped" and store.pointer(repo, "channels/canary").commit == shas[1]
    rec = json.loads(canary.run_path(store, repo, "2026-10-05").read_text())
    assert rec["outcome"] == "shipped" and rec["earlier"][0]["outcome"] == "error"


@pytest.mark.parametrize("break_it", ["probe-not-ok", "missing-stage", "no-digest", "lost"])
def test_finish_recomputes_the_verdict(world, break_it):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    st = passed(repo, shas[1])                       # the worker still claims ok: true
    if break_it == "probe-not-ok":
        st["stages"][3]["ok"] = False
    elif break_it == "missing-stage":
        del st["stages"][2]
    elif break_it == "no-digest":
        st["digest"] = ""
    else:
        st["stages"] = []
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), st, "2026-10-05")
    assert run.outcome == ("held" if break_it == "probe-not-ok" else "error")
    assert store.pointer(repo, "channels/canary").commit == ""


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
    ok, why = canary.check_probes(dep([]), [])                 # deployed, but no probe ran at all
    assert not ok and "no probe ran" in why


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


def test_daily_report(world):
    from qqrelease import report
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), passed(repo, shas[1]), "2026-10-05",
                  run_url="https://example.invalid/runs/1")
    text = report.build(cfg, store, "2026-10-05", [{"number": 7, "title": "Canary held | x", "url": "u"}])
    assert f"| {repo} | **shipped** | `{shas[1][:12]}`" in text
    assert "[#7](u) Canary held / x" in text
    others = [r for r in canary.canary_repos(cfg) if r != repo]
    for r in others:
        assert f"| {r} | **no run** |" in text
    assert "Could not read" in report.build(cfg, store, "2026-10-05", None)
    path = report.write(cfg, store, "2026-10-05", [])
    assert path.read_text().startswith("# Canary report 2026-10-05")


def test_the_report_shows_errors_and_unreadable_records_and_keeps_text_inert(world):
    from qqrelease import report
    cfg, store, mirror, repo, shas = world
    other = next(r for r in canary.canary_repos(cfg) if r != repo)
    lkgr_to(store, mirror, repo, shas[1])
    bad = passed(repo, shas[1])
    bad["stages"][3] = {"name": "deploy-probe", "ok": False, "seconds": 1,
                        "detail": "<!-- @octocat <img src=x> #1 `x`"}
    canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-05")
    p = canary.run_path(store, other, "2026-10-05")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    text = report.build(cfg, store, "2026-10-05", [])
    assert f"| {repo} | **held** |" in text and f"| {other} | **unreadable** |" in text
    assert "<!--" not in text and "<img" not in text and "@octocat" not in text and "#1 " not in text
    canary.finish(cfg, store, mirror, canary.Selection(repo, "build", shas[2], shas[1], ""), None, "2026-10-06")
    text = report.build(cfg, store, "2026-10-06", [])
    assert f"| {repo} | **error** |" in text and "1 pipeline error" in text
    many = [{"number": i, "title": "t", "url": "u"} for i in range(report.OPEN_LIMIT)]
    assert "possibly more" in report.build(cfg, store, "2026-10-06", many)


def test_a_stage_that_could_not_run_is_an_error_not_a_hold(world, tmp_path, monkeypatch):
    cfg, store, mirror, repo, shas = world
    src = tmp_path / "src"
    (src / "infra").mkdir(parents=True)
    (src / "infra" / "repo.toml").write_text("")
    calls, real = [], subprocess.run

    def fake_run(cmd, **kw):
        if "qqrecipes.cli" not in cmd:
            return real(cmd, **kw)                                  # git, for the state store
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, 1)          # a lost or stuck worker
        return subprocess.CompletedProcess(cmd, 3, "", "Traceback: ModuleNotFoundError")

    monkeypatch.setattr(canary.subprocess, "run", fake_run)
    doc = canary.run_stages(cfg, repo, shas[1], src, tmp_path / "out", [], "2026-10-05")
    assert doc["stages"][0]["ran"] is False and "timed out" in doc["stages"][0]["detail"]
    lkgr_to(store, mirror, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), doc, "2026-10-05")
    assert run.outcome == "error" and not canary.held_path(store, repo, shas[1]).is_file()
    doc = canary.run_stages(cfg, repo, shas[1], src, tmp_path / "out2", [], "2026-10-05")
    assert doc["stages"][0]["ran"] is False and "left no results" in doc["stages"][0]["detail"]


def test_exit_codes_decide_whether_a_stage_ran(tmp_path, monkeypatch):
    out = tmp_path / "o"
    out.mkdir()

    def runs(rc, records):
        (out / "results.json").write_text(json.dumps(records))
        monkeypatch.setattr(canary.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, "", ""))
        return canary._recipes("test", tmp_path, out, [])

    assert runs(0, [])[0] == 0
    assert runs(1, [{"exit_code": 1}])[0] == 1                         # ran and failed: a verdict
    assert runs(1, [{"deployment": {"ready": True, "probes": [{"ok": False}]}}])[0] == 1
    for rc, recs in ((1, [{"exit_code": 0}]), (2, [{"exit_code": 1}]), (1, {"x": 1})):
        with pytest.raises(canary.CouldNotRun):
            runs(rc, recs)


def test_the_held_stage_and_digest_come_from_the_verdict(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    bad = passed(repo, shas[1], digest="sha256:" + "a" * 64 + "\n")
    bad["stages"] = [{"name": "junk", "ok": False, "detail": "x", "seconds": 0}] + bad["stages"]
    bad["stages"][1]["ok"] = False                                      # build failed
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-05")
    assert run.outcome == "held" and run.reason.startswith("held at build")
    assert [s["name"] for s in run.stages][0] == "build" and run.digest == ""
