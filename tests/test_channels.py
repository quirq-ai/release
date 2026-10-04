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
    with pytest.raises(ReleaseError, match="needs approval"):
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



def ship(cfg, store, mirror, repo, sha, dig):
    lkgr_to(store, mirror, repo, sha)
    return channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", sha, dig))


def test_rolling_back_twice_goes_further_back_never_forward(world):
    cfg, store, mirror, repo, shas, d = world
    D3 = "sha256:" + "3" * 64
    for sha, dig in zip(shas, (D1, D2, D3)):
        ship(cfg, store, mirror, repo, sha, dig)
    channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary"))
    _, ptr = channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary"))
    assert (ptr.commit, ptr.digest) == (shas[0], D1)
    with pytest.raises(ReleaseError, match="no previous value"):
        channels.plan_rollback(cfg, store, repo, "canary")


def test_a_rolled_back_commit_is_not_shipped_again(world):
    cfg, store, mirror, repo, shas, d = world
    ship(cfg, store, mirror, repo, shas[0], D1)
    ship(cfg, store, mirror, repo, shas[1], D2)
    channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary"))
    with pytest.raises(ReleaseError, match="rolled back from"):
        channels.plan_promote(cfg, store, repo, "canary", shas[1], D2)


def test_a_promotion_that_changes_nothing_is_refused(world):
    cfg, store, mirror, repo, shas, d = world
    ship(cfg, store, mirror, repo, shas[0], D1)
    with pytest.raises(ReleaseError, match="already names"):
        channels.plan_promote(cfg, store, repo, "canary", shas[0], D1)


def test_retrying_a_rollback_whose_write_landed_does_not_roll_back_again(world, config_root, tmp_path):
    cfg, store, mirror, repo, shas, d = world
    ship(cfg, store, mirror, repo, shas[0], D1)
    ship(cfg, store, mirror, repo, shas[1], D2)
    op = channels.plan_rollback(cfg, store, repo, "canary")

    class Timeout:
        def write_ref(self, *a):
            mirror.write_ref(*a)
            raise ReleaseError("timed out")

    with pytest.raises(ReleaseError):
        channels.apply(store, Timeout(), op)
    rc = cli.main(["channel", "rollback", "--config", str(config_root), "--state", str(store.root),
                   "--backend", "local", "--target-root", str(tmp_path / "targets"), "--repo", repo,
                   "--channel", "canary"])
    ptr = store.pointer(repo, "channels/canary")
    assert rc == 0 and (ptr.commit, ptr.digest) == (shas[0], D1)
    assert git("rev-parse", "refs/heads/channels/canary", cwd=d) == shas[0]
    published = json.loads((store.root / "channels.json").read_text())["repos"][repo]["canary"]
    assert published["commit"] == shas[0]          # settling rewrites the manifest too


def test_a_rerun_rollback_with_from_is_refused(world):
    cfg, store, mirror, repo, shas, _ = world
    ship(cfg, store, mirror, repo, shas[0], D1)
    ship(cfg, store, mirror, repo, shas[1], D2)
    channels.apply(store, mirror, channels.plan_rollback(cfg, store, repo, "canary", from_commit=shas[1]))
    with pytest.raises(ReleaseError, match="has moved"):              # the double click
        channels.plan_rollback(cfg, store, repo, "canary", from_commit=shas[1])
    assert store.pointer(repo, "channels/canary").commit == shas[0]


def test_retrying_a_promote_whose_write_landed_exits_0(world, config_root, tmp_path):
    cfg, store, mirror, repo, shas, d = world
    lkgr_to(store, mirror, repo, shas[0])
    op = channels.plan_promote(cfg, store, repo, "canary", shas[0], D1)

    class Timeout:
        def write_ref(self, *a):
            mirror.write_ref(*a)
            raise ReleaseError("timed out")

    with pytest.raises(ReleaseError):
        channels.apply(store, Timeout(), op)
    rc = cli.main(["channel", "promote", "--config", str(config_root), "--state", str(store.root),
                   "--backend", "local", "--target-root", str(tmp_path / "targets"), "--repo", repo,
                   "--channel", "canary", "--commit", shas[0], "--digest", D1])
    assert rc == 0 and store.pointer(repo, "channels/canary").commit == shas[0]


def test_rules_that_need_a_person_or_a_signal_are_refused(world):
    cfg, store, *_ = world
    for name in ("dev", "stable"):
        with pytest.raises(ReleaseError, match="v0 cannot check"):
            channels.check_automatic(cfg, name)
    channels.check_automatic(cfg, "canary")
    cfg["channels"]["channel"][2].setdefault("rollback", {})["approval"] = "human-owner"
    stable = cfg["channels"]["channel"][2]["name"]
    with pytest.raises(ReleaseError, match="needs approval"):
        channels.plan_rollback(cfg, store, world[3], stable)


def test_dev_only_takes_the_artifact_canary_vetted(world):
    cfg, store, mirror, repo, shas, d = world
    ship(cfg, store, mirror, repo, shas[0], D1)
    dev = next(c for c in cfg["channels"]["channel"] if c["name"] == "dev")
    dev["promotion"] = {"approval": "none"}          # as if v1's checks existed
    with pytest.raises(ReleaseError, match="only an artifact the channel before vetted"):
        channels.plan_promote(cfg, store, repo, "dev", shas[0], D2)
    assert channels.plan_promote(cfg, store, repo, "dev", shas[0], D1).to_commit == shas[0]


def test_canary_may_ship_what_lkgr_named_when_it_started(world):
    """lkgr advanced during the build: the built commit is still known good."""
    cfg, store, mirror, repo, shas, _ = world
    lkgr_to(store, mirror, repo, shas[1])
    lkgr_to(store, mirror, repo, shas[2])
    _, ptr = channels.apply(store, mirror, channels.plan_promote(cfg, store, repo, "canary", shas[1], D1))
    assert ptr.commit == shas[1]


def test_canary_may_not_ship_a_commit_lkgr_retreated_from(world):
    cfg, store, mirror, repo, shas, _ = world
    lkgr_to(store, mirror, repo, shas[1])
    lkgr_to(store, mirror, repo, shas[2])
    executor.move(store, mirror, executor.plan(store, "retreat", repo, "lkgr", shas[1]))
    with pytest.raises(ReleaseError, match="not it"):
        channels.plan_promote(cfg, store, repo, "canary", shas[2], D1)
    lkgr_to(store, mirror, repo, shas[2])          # forward again: shas[1] is behind a retreat
    with pytest.raises(ReleaseError, match="not it"):
        channels.plan_promote(cfg, store, repo, "canary", shas[0], D1)


def test_nothing_the_installer_would_refuse_is_published(world):
    cfg, store, mirror, repo, shas, _ = world
    lkgr_to(store, mirror, repo, shas[0])
    with pytest.raises(ReleaseError, match="not sha256"):
        channels.plan_promote(cfg, store, repo, "canary", shas[0], D1 + "\n")     # re.match took this
    with pytest.raises(ReleaseError, match="40-character"):
        channels.plan_promote(cfg, store, repo, "canary", shas[0].upper(), D1)
    for bad in (".github", "x" * 101, "café"):
        with pytest.raises(ReleaseError, match="installer accepts"):
            channels.check_repo(cfg, bad, "canary")
    ship(cfg, store, mirror, repo, shas[0], D1)
    good = store.pointer(repo, "channels/canary")
    import dataclasses
    with pytest.raises(ReleaseError, match="installer refuses"):
        channels.manifest_json(store, dataclasses.replace(good, digest=D1 + "\n"))
