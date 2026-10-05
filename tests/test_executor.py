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

    def write_ref(self, repo, ref, expected, new):
        recorded = list((self.store.root / "ops").glob("*.json"))
        assert recorded, "the operation key must be recorded before the effect"
        committed = git("log", "--format=%s", cwd=self.store.root)
        assert "record " in committed, "the record must be committed before the effect"
        assert self.store.pointer(repo, ref).pending, "the pointer must say it waits on the operation"
        self.seen.append((ref, tuple(expected), new))
        return self.inner.write_ref(repo, ref, expected, new)


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


def test_a_run_that_died_before_the_write_is_abandoned_then_planned_again(target, store):
    root, shas = target
    op = executor.plan(store, "advance", "demo", "lkgr", shas[2])

    class Crash:
        def write_ref(self, *a):
            raise KeyboardInterrupt  # the runner died after recording

    with pytest.raises(KeyboardInterrupt):
        executor.move(store, Crash(), op)
    assert store.op(op.key).state == "recorded" and store.pointer("demo", "lkgr").pending == op.key
    mirror = load("local", target_root=root)
    with pytest.raises(ReleaseError, match="settle it first"):
        executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    settled = executor.finish_pending(store, mirror, "demo", "lkgr")
    assert settled.state == "abandoned" and not store.pointer("demo", "lkgr").pending
    again = executor.plan(store, "advance", "demo", "lkgr", shas[2])
    assert again.key == op.key           # the same intent, recorded afresh
    done, ptr = executor.move(store, mirror, again)
    assert done.state == "applied" and ptr.commit == shas[2]


def test_the_ref_moved_by_someone_else_fails_the_operation(target, store):
    root, shas = target
    git("update-ref", "refs/heads/lkgr", shas[0], cwd=root / "demo")   # not the executor
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])
    with pytest.raises(ReleaseError, match="something else moved it"):
        executor.move(store, load("local", target_root=root), op)
    assert store.op(op.key).state == "failed"
    ptr = store.pointer("demo", "lkgr")
    assert ptr.commit == "" and ptr.pending == op.key


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


class Skip:
    """No executor identity yet: the ref is never written."""
    def write_ref(self, repo, ref, expected, new):
        return "skipped: no release executor identity"


def test_skipped_moves_then_an_identity_takes_over(target, store):
    """Moves recorded while no identity existed must not wedge the first real write (the ref is
    absent, not at the pointer's commit)."""
    root, shas = target
    for sha in shas[:2]:
        executor.move(store, Skip(), executor.plan(store, "advance", "demo", "lkgr", sha))
    assert store.pointer("demo", "lkgr").mirrored is False
    op, ptr = executor.move(store, load("local", target_root=root),
                            executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    assert op.mirror == "pushed" and ptr.mirrored
    assert git("rev-parse", "refs/heads/lkgr", cwd=root / "demo") == shas[2]


def test_a_mirrored_ref_must_be_where_the_pointer_is(target, store):
    """Once the ref is known to match the pointer, an older value means someone else wrote it."""
    root, shas = target
    mirror = load("local", target_root=root)
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[1]))
    git("update-ref", "refs/heads/lkgr", shas[0], cwd=root / "demo")
    with pytest.raises(ReleaseError, match="something else moved it"):
        executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))


def test_a_crash_after_the_ref_moved_is_finished_before_moving_on(target, store):
    """The ref moved to Y, the run died before recording it; meanwhile Z became the candidate. The
    next run must first record Y, then move Y -> Z, never wedge on the CAS."""
    root, shas = target
    mirror = load("local", target_root=root)
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[0]))
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])

    class LandsThenDies:
        def write_ref(self, *a):
            mirror.write_ref(*a)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        executor.move(store, LandsThenDies(), op)
    assert store.pointer("demo", "lkgr").commit == shas[0]       # the store does not know yet
    with pytest.raises(ReleaseError, match="settle it first"):
        executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    finished = executor.finish_pending(store, mirror, "demo", "lkgr")
    assert finished.key == op.key and finished.state == "applied" and finished.mirror == "already there"
    _, ptr = executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[2]))
    assert ptr.commit == shas[2] and not ptr.pending
    assert git("rev-parse", "refs/heads/lkgr", cwd=root / "demo") == shas[2]


def test_a_write_that_landed_but_reported_failure_is_finished(target, store):
    """A timeout after the forge applied the update: the op is saved failed, the pointer pending."""
    root, shas = target
    mirror = load("local", target_root=root)
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])

    class Timeout:
        def write_ref(self, *a):
            mirror.write_ref(*a)
            raise ReleaseError("timed out")

    with pytest.raises(ReleaseError, match="timed out"):
        executor.move(store, Timeout(), op)
    assert store.op(op.key).state == "failed"
    done = executor.finish_pending(store, mirror, "demo", "lkgr")
    assert done.state == "applied" and store.pointer("demo", "lkgr").commit == shas[1]


def test_settling_never_writes_a_stale_target(target, store):
    """A pending move to c2 that never landed, and c2 has turned red since: settling must not write
    c2 (the verdicts it was planned on are stale); it abandons the op and lkgr is planned again."""
    root, shas = target
    mirror = load("local", target_root=root)
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[0]))
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])

    class BadGateway:
        def write_ref(self, *a):
            raise ReleaseError("HTTP 502")

    with pytest.raises(ReleaseError):
        executor.move(store, BadGateway(), op)
    spy = Spy(mirror, store)
    spy.can_write = mirror.can_write
    spy.read_ref = mirror.read_ref
    settled = executor.finish_pending(store, spy, "demo", "lkgr")
    assert settled.state == "abandoned" and spy.seen == []
    assert git("rev-parse", "refs/heads/lkgr", cwd=root / "demo") == shas[0]
    assert store.pointer("demo", "lkgr").commit == shas[0]


def test_settling_without_an_identity_abandons(store):
    mirror = load("github", repos={"demo": "quirq-ai/demo"}, token="")
    mirror.read_ref = lambda repo, ref: ""
    op = executor.plan(store, "advance", "demo", "lkgr", "a" * 40)

    class Crash:
        def write_ref(self, *a):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        executor.move(store, Crash(), op)
    assert executor.finish_pending(store, mirror, "demo", "lkgr").state == "abandoned"


def test_a_ref_moved_by_someone_else_keeps_the_operation_pending(target, store):
    root, shas = target
    mirror = load("local", target_root=root)
    executor.move(store, mirror, executor.plan(store, "advance", "demo", "lkgr", shas[0]))
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])

    class Dies:
        def write_ref(self, *a):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        executor.move(store, Dies(), op)
    git("update-ref", "refs/heads/lkgr", shas[2], cwd=root / "demo")     # not the executor
    with pytest.raises(ReleaseError, match="something else moved it"):
        executor.finish_pending(store, mirror, "demo", "lkgr")
    assert store.pointer("demo", "lkgr").pending == op.key


def test_a_landed_write_is_found_by_a_run_without_an_identity(target, store):
    root, shas = target
    local = load("local", target_root=root)
    op = executor.plan(store, "advance", "demo", "lkgr", shas[1])

    class Timeout:
        def write_ref(self, *a):
            local.write_ref(*a)
            raise ReleaseError("timed out")

    with pytest.raises(ReleaseError):
        executor.move(store, Timeout(), op)

    class ReadOnly:
        read_ref = local.read_ref
        def can_write(self):
            return False

    assert executor.finish_pending(store, ReadOnly(), "demo", "lkgr").state == "applied"
    assert store.pointer("demo", "lkgr").commit == shas[1]


def test_a_push_race_with_the_other_writer_is_rebased(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "release-state", str(remote), cwd=tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        git("clone", "-q", str(remote), str(d), cwd=tmp_path)
        git("checkout", "-q", "-b", "release-state", cwd=d)
    sa, sb = Store(a, push=True), Store(b, push=True)
    sa.save({a / "pointers" / "x" / "lkgr.json": "{}\n"}, "lkgr")
    sb.save({b / "pointers" / "x" / "channels" / "canary.json": "{}\n"}, "canary")   # behind: rebases
    log = git("--git-dir", str(remote), "log", "--format=%s", "release-state", cwd=tmp_path)
    assert log.splitlines() == ["canary", "lkgr"]
    sb.save({b / "pointers" / "x" / "lkgr.json": "{\"other\": 1}\n"}, "conflict")
    with pytest.raises(ReleaseError, match="conflict"):
        sa.save({a / "pointers" / "x" / "lkgr.json": "{\"mine\": 1}\n"}, "mine")
    # S7: the refused commit is gone, so the next save publishes only its own change.
    assert json.loads((a / "pointers" / "x" / "lkgr.json").read_text()) == {"other": 1}
    sa.save({a / "pointers" / "y" / "lkgr.json": "{}\n"}, "next")
    log = git("--git-dir", str(remote), "log", "--format=%s", "release-state", cwd=tmp_path)
    assert log.splitlines() == ["next", "conflict", "canary", "lkgr"]


def test_no_repository_hook_runs_while_the_store_commits_rebases_or_pushes(tmp_path):
    """A hook planted earlier in a writer job would run with the push credential."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "release-state", str(remote), cwd=tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        git("clone", "-q", str(remote), str(d), cwd=tmp_path)
        git("checkout", "-q", "-b", "release-state", cwd=d)
    ran = tmp_path / "ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push", "pre-rebase", "post-rewrite",
                 "reference-transaction"):
        hook = b / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{ran}'\n")
        hook.chmod(0o755)
    Store(a, push=True).save({a / "pointers" / "x" / "lkgr.json": "{}\n"}, "lkgr")
    Store(b, push=True).save({b / "pointers" / "x" / "channels" / "canary.json": "{}\n"}, "canary")
    log = git("--git-dir", str(remote), "log", "--format=%s", "release-state", cwd=tmp_path)
    assert log.splitlines() == ["canary", "lkgr"]   # b committed, rebased and pushed
    assert not ran.exists()


def test_a_rebase_never_merges_two_writers_changes_to_one_file(tmp_path):
    """A compare-and-swap per file: lines far apart would merge cleanly, into a record nobody wrote."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "release-state", str(remote), cwd=tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        git("clone", "-q", str(remote), str(d), cwd=tmp_path)
        git("checkout", "-q", "-b", "release-state", cwd=d)
    sa, sb = Store(a, push=True), Store(b, push=True)
    lines = [f"line {i}" for i in range(20)]
    sa.save({a / "rec.txt": "\n".join(lines) + "\n"}, "base")
    git("pull", "-q", "origin", "refs/heads/release-state", cwd=b)
    sa.save({a / "rec.txt": "\n".join(["first"] + lines[1:]) + "\n"}, "a")
    with pytest.raises(ReleaseError, match="another writer changed rec.txt"):
        sb.save({b / "rec.txt": "\n".join(lines[:-1] + ["last"]) + "\n"}, "b")
    assert (b / "rec.txt").read_text().startswith("first")


def test_a_refused_push_never_rides_along_with_the_next_save(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "release-state", str(remote), cwd=tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    "  git diff --name-only $old $new 2>/dev/null | grep -q huge && exit 1\ndone\nexit 0\n")
    hook.chmod(0o755)
    a = tmp_path / "a"
    git("clone", "-q", str(remote), str(a), cwd=tmp_path)
    git("checkout", "-q", "-b", "release-state", cwd=a)
    sa = Store(a, push=True)
    sa.save({a / "first.json": "{}\n"}, "first")
    with pytest.raises(ReleaseError):
        sa.save({a / "huge.json": "{}\n"}, "refused")
    assert not (a / "huge.json").exists()
    sa.save({a / "other.json": "{}\n"}, "other repo's record")
    log = git("--git-dir", str(remote), "log", "--format=%s", "release-state", cwd=tmp_path)
    assert log.splitlines() == ["other repo's record", "first"]


def test_a_tag_named_like_the_state_branch_is_never_read(tmp_path):
    """git resolves a short name to a tag before a branch; the state branch is always named in full."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "release-state", str(remote), cwd=tmp_path)
    a, b, forger = tmp_path / "a", tmp_path / "b", tmp_path / "forger"
    for d in (a, b, forger):
        git("clone", "-q", str(remote), str(d), cwd=tmp_path)
        git("checkout", "-q", "-b", "release-state", cwd=d)
    (forger / "channels.json").write_text("forged\n")
    git("add", "-A", cwd=forger)
    git("-c", "user.name=f", "-c", "user.email=f@x.invalid", "commit", "-q", "-m", "forged", cwd=forger)
    git("tag", "release-state", cwd=forger)
    git("push", "-q", "origin", "refs/tags/release-state", cwd=forger)
    Store(a, push=True).save({a / "pointers" / "x" / "lkgr.json": "{}\n"}, "lkgr")
    Store(b, push=True).save({b / "pointers" / "x" / "channels" / "canary.json": "{}\n"}, "canary")
    assert not (b / "channels.json").exists()                   # rebased onto the branch, not the tag
    log = git("--git-dir", str(remote), "log", "--format=%s", "refs/heads/release-state", cwd=tmp_path)
    assert log.splitlines() == ["canary", "lkgr"]
