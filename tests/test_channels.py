import argparse
import json
import subprocess

import pytest

from qqrelease import channels, cli, executor
from qqrelease.backends import load
from qqrelease.errors import ReleaseError
from qqrelease.store import Store

D1, D2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, cfg):
    repo = cfg["repos"]["repo"][0]["name"]
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
    store = Store(state)
    mirror = load("local", target_root=tmp_path / "targets")
    return cfg, store, mirror, repo, shas, d


def lkgr_to(store, mirror, repo, sha):
    executor.move(store, mirror, executor.plan(store, "advance", repo, "lkgr", sha))


def test_promote_takes_lkgrs_commit_and_names_a_digest(world):
    cfg, store, mirror, repo, shas, d = world
    lkgr_to(store, mirror, repo, shas[1])
    op, ptr = channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", shas[1], D1))
    assert (ptr.commit, ptr.digest, op.kind) == (shas[1], D1, "promote")
    assert git("rev-parse", "refs/heads/channels/canary", cwd=d) == shas[1]
    published = json.loads((store.root / "channels.json").read_text())
    assert published["schema"] == "qq-channels/1"
    assert published["repos"][repo]["canary"]["digest"] == D1
    assert "lkgr" not in published["repos"][repo]


def test_promote_refuses_anything_but_the_source_commit(world):
    cfg, store, mirror, repo, shas, _ = world
    with pytest.raises(ReleaseError, match="no lkgr yet"):
        channels.plan_promote(cfg, store, repo, "canary", shas[1], D1)
    lkgr_to(store, mirror, repo, shas[1])
    with pytest.raises(ReleaseError, match="takes its build from lkgr"):
        channels.plan_promote(cfg, store, repo, "canary", shas[2], D1)


def test_promote_needs_a_real_digest(world):
    cfg, store, mirror, repo, shas, _ = world
    lkgr_to(store, mirror, repo, shas[1])
    for bad in ["", "sha256:abc", "md5:" + "1" * 64, shas[1]]:
        with pytest.raises(ReleaseError, match="digest"):
            channels.plan_promote(cfg, store, repo, "canary", shas[1], bad)


def test_dev_takes_its_build_from_canary(world):
    cfg, store, mirror, repo, shas, _ = world
    assert channels.source_ref(cfg, "canary") == "lkgr"
    assert channels.source_ref(cfg, "dev") == "channels/canary"
    lkgr_to(store, mirror, repo, shas[1])
    with pytest.raises(ReleaseError, match="no channels/canary yet"):
        channels.plan_promote(cfg, store, repo, "dev", shas[1], D1)


def test_unknown_channel_and_repo_are_errors(world):
    cfg, store, *_ = world
    with pytest.raises(ReleaseError, match="no channel"):
        channels.channel_cfg(cfg, "beta")
    with pytest.raises(ReleaseError, match="not onboarded"):
        channels.check_repo(cfg, "nope", "canary")


def test_rollback_restores_the_previous_commit_and_digest(world):
    cfg, store, mirror, repo, shas, d = world
    for sha, dig in ((shas[1], D1), (shas[2], D2)):
        lkgr_to(store, mirror, repo, sha)
        channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", sha, dig))
    op, ptr = channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary"))
    assert (op.kind, ptr.commit, ptr.digest) == ("rollback", shas[1], D1)
    assert git("rev-parse", "refs/heads/channels/canary", cwd=d) == shas[1]
    assert json.loads((store.root / "channels.json").read_text())["repos"][repo]["canary"]["commit"] == shas[1]


def test_rollback_with_nothing_before_is_refused(world):
    cfg, store, mirror, repo, shas, _ = world
    with pytest.raises(ReleaseError, match="names nothing yet"):
        channels.plan_rollback(cfg, store, repo, "canary")
    lkgr_to(store, mirror, repo, shas[1])
    channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", shas[1], D1))
    with pytest.raises(ReleaseError, match="no previous value"):
        channels.plan_rollback(cfg, store, repo, "canary")


def test_qq_channel_is_registered_for_depot():
    p = argparse.ArgumentParser(prog="qq")
    sub = p.add_subparsers(dest="command")
    cli.register(sub)
    args = p.parse_args(["channel", "show", "--state", "/nonexistent"])
    assert callable(args.run)


def test_cli_rollback_round_trip(world, config_root, tmp_path):
    cfg, store, mirror, repo, shas, d = world
    for sha, dig in ((shas[1], D1), (shas[2], D2)):
        lkgr_to(store, mirror, repo, sha)
        channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", sha, dig))
    rc = cli.main(["channel", "rollback", "--config", str(config_root), "--state", str(store.root),
                   "--backend", "local", "--target-root", str(tmp_path / "targets"), "--repo", repo,
                   "--channel", "canary"])
    assert rc == 0 and git("rev-parse", "refs/heads/channels/canary", cwd=d) == shas[1]
