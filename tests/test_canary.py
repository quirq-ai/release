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
    # S4: an action that could not start on this machine is never the commit's verdict.
    for recs in ([{"exit_code": 0}, {"exit_code": 127}], [{"exit_code": 126}],
                 [{"capability": "fetch", "name": "toolchain-check", "exit_code": 1}]):
        with pytest.raises(canary.CouldNotRun, match="could not start"):
            runs(1, recs)
    assert runs(1, [{"capability": "test", "name": "toolchain-check", "exit_code": 1}])[0] == 1


def test_a_released_hold_is_built_again_and_a_retry_is_a_noop(world, config_root, capsys):
    from qqrelease import cli
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    bad = passed(repo, shas[1])
    bad["stages"][1]["ok"] = False
    assert canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-05").outcome == "held"
    assert canary.select(cfg, store, repo).action == "noop"
    base = ["canary", "release-hold", "--config", str(config_root), "--state", str(store.root), "--repo", repo]
    why = "runner lost its toolchain"
    for commit, reason, n in ((shas[1][:12], why, "0"), (shas[2], why, "0"), (shas[1], " ", "0"),
                              (shas[1], "x" * 70000, "0"), (shas[1], why, "1"), (shas[1], why, "-1")):
        assert cli.main(base + ["--commit", commit, "--reason", reason, "--released-before", n]) == 2
    assert canary.is_held(store, repo, shas[1])
    first_dispatch = base + ["--commit", shas[1], "--reason", why, "--released-before", "0",
                             "--actor", "https://example.invalid/run/1", "--requested-by", "alice (triggered by bob)"]
    assert cli.main(first_dispatch) == 0
    first = json.loads(canary.held_path(store, repo, shas[1]).read_text())
    rel = first["releases"][0]
    assert rel["requested_by"] == "alice (triggered by bob)" and rel["reason"] == why
    key = rel["operation"]
    op = store.op(key)
    assert (op.kind, op.state, op.from_commit, op.generation) == ("release-hold", "applied", shas[1], 0)
    assert "alice" in op.actor
    assert canary.select(cfg, store, repo).action == "build"                    # built again
    assert cli.main(first_dispatch) == 0                                          # a retried dispatch
    assert "already released" in capsys.readouterr().out
    assert json.loads(canary.held_path(store, repo, shas[1]).read_text()) == first
    # Held again later: the old release stays on record, and it takes a release of its own.
    assert canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-06").outcome == "held"
    again = json.loads(canary.held_path(store, repo, shas[1]).read_text())
    assert again["state"] == "held" and again["releases"] == first["releases"]
    assert canary.select(cfg, store, repo).action == "noop"
    assert cli.main(first_dispatch) == 2                 # S6: re-running the old run never releases this hold
    assert canary.is_held(store, repo, shas[1])
    outcome, key2 = canary.release_hold(store, repo, shas[1], "runner fixed again", 1)
    assert outcome == "released" and key2 != key and store.op(key2).generation == 1
    # A record whose history was edited by hand never reports a release that did not happen.
    hp = canary.held_path(store, repo, shas[1])
    for releases, n in (("oops", 1), ([], 0), ([{**rel, "operation": "f" * 32}], 1)):
        store.save({hp: json.dumps({**again, "state": "held", "releases": releases, "digest": "sha256:" + "e" * 64})},
                   "hand edit")
        with pytest.raises(ReleaseError, match="by hand"):
            canary.release_hold(store, repo, shas[1], "again", n)
        assert canary.is_held(store, repo, shas[1])


def test_errors_that_never_judge_a_commit_end_in_a_hold(world, config_root):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    stuck = passed(repo, shas[1])
    stuck["stages"] = [{"name": "build", "ok": False, "ran": False, "seconds": 1,
                        "detail": "could not run: an action could not start: test:unit (exit 127)"}]
    for i in range(canary.ERROR_LIMIT - 1):
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), stuck, "2026-10-05")
        assert run.outcome == "error" and not canary.is_held(store, repo, shas[1])
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), stuck, "2026-10-06")
    assert run.outcome == "held" and canary.RUNNER_FAULT in run.reason
    rec = json.loads(canary.held_path(store, repo, shas[1]).read_text())
    assert (rec["stage"], rec["tag"], rec["errors"]) == ("build", canary.RUNNER_FAULT, canary.ERROR_LIMIT)
    assert canary.select(cfg, store, repo).action == "noop"
    draft = canary.postmortem_draft(config_root, run)
    assert canary.RUNNER_FAULT in draft and "will not be retried" not in draft
    # A release starts the count again; a lost worker (no stage results) counts too.
    canary.release_hold(store, repo, shas[1], "runner fixed", 0)
    for i in range(canary.ERROR_LIMIT - 1):
        assert canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-07").outcome == "error"
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-07")
    assert run.outcome == "held" and run.stages[0]["name"] == "pipeline"
    assert json.loads(canary.held_path(store, repo, shas[1]).read_text())["stage"] == "pipeline"
    assert canary.postmortem_draft(config_root, run)


def test_stage_results_of_any_shape_are_counted_never_a_crash(world, config_root, tmp_path):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    shapes = ({"repo": repo, "commit": shas[1], "stages": [{"name": 5, "ok": True}]},
              {"repo": repo, "commit": shas[1], "stages": 5}, [1, 2])
    for doc in shapes:
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), doc, "2026-10-05")
    assert run.outcome == "held" and canary.RUNNER_FAULT in run.reason
    # Untrusted rows reach the hold and the postmortem only as plain values.
    lkgr_to(store, mirror, repo, shas[2])
    bare = {"repo": repo, "commit": shas[2], "stages": [{"name": "build"}]}
    for i in range(canary.ERROR_LIMIT):
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bare, "2026-10-06")
    assert run.outcome == "held" and run.stages[0]["name"] == "build"
    assert "| build | **fail** |" in canary.postmortem_draft(config_root, run)
    # Another commit's results never name this commit's stage.
    lkgr_to(store, mirror, repo, shas[0])
    other = {**passed(repo, shas[1]), "stages": [{"name": "verify", "ok": False, "ran": False}]}
    for i in range(canary.ERROR_LIMIT):
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), other, "2026-10-07")
    assert json.loads(canary.held_path(store, repo, shas[0]).read_text())["stage"] == "pipeline"


def test_huge_numbers_in_stage_results_are_counted(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    huge = int("9" * 400)
    for ok in (True, False):
        doc = {"repo": repo, "commit": shas[1], "stages": [{"name": "build", "ok": ok, "seconds": huge}]}
        rows = canary._stage_rows(doc, canary.select(cfg, store, repo))
        assert rows[0]["seconds"] == 0.0
    doc = {"repo": repo, "commit": shas[1], "stages": [{"name": "build", "ok": True, "seconds": huge}]}
    for i in range(canary.ERROR_LIMIT):
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), doc, "2026-10-05")
    assert run.outcome == "held"


def test_finish_survives_unreadable_stage_results_and_keeps_other_holds(world, config_root, tmp_path):
    from qqrelease import cli
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    sel = canary.select(cfg, store, repo)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"repo": repo, "action": "build", "commit": sel.commit,
                                 "previous": sel.previous, "reason": sel.reason}]))
    (tmp_path / "st" / repo).mkdir(parents=True)
    stages_file = tmp_path / "st" / repo / "stages.json"
    contents = ("not json", "[" * 200000, json.dumps({"repo": repo, "commit": shas[1],
                "stages": [{"name": "build", "ok": False, "seconds": int("9" * 400)}]}))
    args = ["canary", "finish", "--config", str(config_root), "--state", str(store.root), "--backend", "local",
            "--target-root", str(tmp_path / "targets"), "--plan", str(plan), "--stages-dir", str(tmp_path / "st"),
            "--held-out", str(tmp_path / "held.json")]
    for i in range(canary.ERROR_LIMIT):
        stages_file.write_text(contents[i])
        rc = cli.main(args + ["--date", f"2026-10-0{5 + i}"])
    held = json.loads((tmp_path / "held.json").read_text())
    # The third run is a real failure (ok: false), so it is held at build like any failure.
    assert rc == 0 and held[0]["stage"] == "build" and canary.is_held(store, repo, shas[1])
    assert json.loads(canary.errors_path(store, repo, shas[1]).read_text())["count"] == 2


def test_a_rerun_of_the_same_run_counts_once_and_a_promote_failure_never(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    for i in range(canary.ERROR_LIMIT + 2):              # the same run's finish job, re-run
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), None, "2026-10-05",
                            run_url="https://example.invalid/runs/7")
    assert run.outcome == "error" and not canary.is_held(store, repo, shas[1])
    assert json.loads(canary.errors_path(store, repo, shas[1]).read_text())["count"] == 1

    class Flaky:                                         # a reply that could not be read
        def __getattr__(self, name):
            return getattr(mirror, name)

        def write_ref(self, *a, **kw):
            raise RuntimeError("reply unreadable")

    lkgr_to(store, mirror, repo, shas[2])
    for i in range(canary.ERROR_LIMIT):
        run = canary.finish(cfg, store, Flaky(), canary.select(cfg, store, repo), passed(repo, shas[2]),
                            "2026-10-06")
        assert run.outcome == "error" and "promote failed: RuntimeError" in run.reason
    assert not canary.errors_path(store, repo, shas[2]).exists() and not canary.is_held(store, repo, shas[2])


def test_worker_text_is_clipped_before_it_reaches_a_record(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    sel = canary.select(cfg, store, repo)
    run = canary.finish(cfg, store, mirror, sel, {"repo": repo, "commit": shas[1], "skip": "x" * 10**6},
                        "2026-10-05")
    assert run.outcome == "noop" and len(run.reason) < canary.TEXT_LIMIT + 100
    names = [{"name": "y" * 10**5, "ok": True}] * 50
    run = canary.finish(cfg, store, mirror, sel, {"repo": repo, "commit": shas[1], "stages": names}, "2026-10-05")
    assert run.outcome == "error" and len(run.reason) < canary.TEXT_LIMIT + 200
    assert canary.run_path(store, repo, "2026-10-05").stat().st_size < 50000


def test_the_number_of_stage_rows_is_bounded(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    many = {"repo": repo, "commit": shas[1], "digest": "sha256:" + "a" * 64,
            "stages": [{"name": "build", "ok": False, "detail": "z" * 30000}] * 500}
    assert canary.verdict(many, canary.select(cfg, store, repo))[0] == "missing"
    for i in range(canary.ERROR_LIMIT):
        run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), many, "2026-10-05")
    assert run.outcome == "held" and len(run.stages) == 1
    assert canary.run_path(store, repo, "2026-10-05").stat().st_size < 200000


def test_a_hold_record_from_before_releases_still_holds(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    store.save({canary.held_path(store, repo, shas[1]): json.dumps({"repo": repo, "commit": shas[1]})}, "old hold")
    assert canary.is_held(store, repo, shas[1]) and canary.select(cfg, store, repo).action == "noop"


def test_the_held_stage_and_digest_come_from_the_verdict(world):
    cfg, store, mirror, repo, shas = world
    lkgr_to(store, mirror, repo, shas[1])
    bad = passed(repo, shas[1], digest="sha256:" + "a" * 64 + "\n")
    bad["stages"] = [{"name": "junk", "ok": False, "detail": "x", "seconds": 0}] + bad["stages"]
    bad["stages"][1]["ok"] = False                                      # build failed
    run = canary.finish(cfg, store, mirror, canary.select(cfg, store, repo), bad, "2026-10-05")
    assert run.outcome == "held" and run.reason.startswith("held at build")
    assert [s["name"] for s in run.stages][0] == "build" and run.digest == ""
