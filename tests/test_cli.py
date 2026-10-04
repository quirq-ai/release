import json
import subprocess

from qqrelease import cli, config


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_one_repos_error_does_not_stop_the_others(tmp_path, cfg, config_root, capsys):
    repos = config.repos(cfg)
    broken, ok = repos[0], repos[1]
    d = tmp_path / "targets" / ok.name            # no target repo for `broken`: its write fails
    d.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=d)
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "c",
        cwd=d)
    sha = git("rev-parse", "HEAD", cwd=d)
    snap = {"repos": {r.name: {
        "commits": [{"sha": sha, "landed_at": "2026-10-04T10:00:00Z", "title": "c"}],
        "runs": [{"builder": b, "commit": sha, "status": "completed", "conclusion": "success", "id": "1"}
                 for b in r.postsubmit]} for r in (broken, ok)}}
    (tmp_path / "snap.json").write_text(json.dumps(snap))
    state = tmp_path / "state"
    state.mkdir()
    rc = cli.main(["lkgr", "--config", str(config_root), "--state", str(state), "--backend", "local",
                   "--target-root", str(tmp_path / "targets"), "--snapshot", str(tmp_path / "snap.json"),
                   "--now", "2026-10-04T12:00:00Z"])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"| {broken.name} | **error** |" in out
    assert f"| {ok.name} | **advance** |" in out
    assert git("rev-parse", "refs/heads/lkgr", cwd=d) == sha
