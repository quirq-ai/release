import json
import subprocess

import pytest

from qqrelease import executor
from qqrelease.backends import load
from qqrelease.errors import ReleaseError
from qqrelease.store import Store


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def target(tmp_path):
    """A target repo `demo` with three commits on main."""
    d = tmp_path / "targets" / "demo"
    d.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=d)
    shas = []
    for i in range(3):
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty",
            "-m", f"c{i}", cwd=d)
        shas.append(git("rev-parse", "HEAD", cwd=d))
    return tmp_path / "targets", shas


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    git("init", "-q", "-b", "release-state", cwd=d)
    return Store(d)


class Spy:
    """Wraps a backend and checks the operation is in the store before the ref moves."""
    def __init__(self, inner, store):
        self.inner, self.store, self.seen = inner, store, []

    def write_ref(self, repo, ref, old, new):
        recorded = list((self.store.root / "ops").glob("*.json"))
        assert recorded, "the operation key must be recorded before the effect"
        committed = git("log", "--format=%s", cwd=self.store.root)
        assert "record " in committed, "the record must be committed before the effect"
        self.seen.append((ref, old, new))
        return self.inner.write_ref(repo, ref, old, new)


def test_move_records_the_key_first_then_moves_the_ref(target, store):
    root, shas = target
    mirror = Spy(load("local", target_root=root), store)
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])
    op, ptr = executor.move(store, mirror, op, at="2026-10-04T12:00:00Z")
    assert op.state == "applied" and op.mirror == "pushed"
    assert git("rev-parse", "refs/heads/lkgr", cwd=root / "demo") == shas[1]
    assert (ptr.commit, ptr.generation, ptr.op) == (shas[1], 1, op.key)
    on_disk = json.loads(store.op_path(op.key).read_text())
    assert on_disk["state"] == "applied"


def test_a_retry_of_the_same_intent_does_nothing_twice(target, store):
    root, shas = target
    mirror = Spy(load("local", target_root=root), store)
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])
    executor.move(store, mirror, op)
    # The same intent planned before the first move landed has the same key: a no-op.
    again = executor.Operation(kind="advance", repo="demo", ref="lkgr", from_commit="", to_commit=shas[1],
                               generation=0)
    assert again.key == op.key
    done, ptr = executor.move(store, mirror, again)
    assert done.state == "applied" and ptr.generation == 1 and len(mirror.seen) == 1


def test_a_recorded_operation_is_finished_after_a_crash(target, store):
    root, shas = target
    op = executor.plan(store, "advance", "demo", "lkgr", shas[2])

    class Crash:
        def write_ref(self, *a):
            raise KeyboardInterrupt  # the runner died after recording

    with pytest.raises(KeyboardInterrupt):
        executor.move(store, Crash(), op)
    assert store.op(op.key).state == "recorded"
    again = executor.plan(store, "advance", "demo", "lkgr", shas[2])
    assert again.key == op.key
    done, ptr = executor.move(store, load("local", target_root=root), again)
    assert done.state == "applied" and ptr.commit == shas[2]


def test_the_ref_moved_by_someone_else_fails_the_operation(target, store):
    root, shas = target
    git("update-ref", "refs/heads/lkgr", shas[0], cwd=root / "demo")   # not the executor
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])
    with pytest.raises(ReleaseError, match="could not move"):
        executor.move(store, load("local", target_root=root), op)
    assert store.op(op.key).state == "failed"
    assert store.pointer("demo", "lkgr").commit == ""


def test_a_stale_plan_is_refused(target, store):
    root, shas = target
    mirror = load("local", target_root=root)
    stale = executor.plan(store, "advance", "demo", "lkgr", shas[2])
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[1]))
    with pytest.raises(ReleaseError, match="moved since"):
        executor.move(store, mirror, stale)


def test_moving_back_to_an_old_commit_later_is_a_new_operation(target, store):
    root, shas = target
    mirror = load("local", target_root=root)
    first, _ = executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[1]))
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    executor.move(store, mirror, executor.plan(store, "retreat", "demo", "lkgr", shas[1]))
    again, ptr = executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    assert again.key != first.key and ptr.generation == 4
    assert [h["commit"] for h in ptr.history] == [shas[1], shas[2], shas[1]]


def test_github_without_an_identity_skips_the_ref_and_says_why(store):
    mirror = load("github", repos={"demo": "quirq-ai/demo"}, token="")
    op, ptr = executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", "a" * 40))
    assert op.mirror.startswith("skipped: no release executor identity")
    assert ptr.commit == "a" * 40


def test_store_names_cannot_escape(store):
    with pytest.raises(ReleaseError):
        store.pointer("..", "lkgr")
    with pytest.raises(ReleaseError):
        store.pointer("demo", "channels/../x")


def test_unknown_backend_is_an_error():
    with pytest.raises(ReleaseError, match="no release backend"):
        load("nope")
