"""The release executor: the only code that moves `lkgr` and `channels/*`.

`move` records the operation key in the state store (and publishes it) before the effect, then
mirrors the pointer into the target repo's git ref through the backend, then records the new
pointer and marks the operation applied. Re-running a move with the same intent is safe:

- an applied operation with the same key is a no-op;
- a recorded one (the last run died mid-way) is re-applied; the backend treats a ref already at
  the target commit as done.
"""
from __future__ import annotations

from datetime import datetime, timezone

from qqrelease.errors import ReleaseError
from qqrelease.operations import Operation, Pointer
from qqrelease.store import Store

# What a backend returns when the target ref names the new commit afterwards.
MIRRORED = ("pushed", "already there")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def plan(store: Store, kind: str, repo: str, ref: str, to_commit: str, digest: str = "",
         reason: str = "", actor: str = "local") -> Operation:
    cur = store.pointer(repo, ref)
    return Operation(kind=kind, repo=repo, ref=ref, from_commit=cur.commit, to_commit=to_commit,
                     generation=cur.generation, digest=digest, reason=reason, actor=actor)


def finish_pending(store: Store, mirror, repo: str, ref: str, at: str | None = None) -> Operation | None:
    """Settle the operation a pointer is waiting on, if any, before planning a new one.

    A run can die (or lose its push, or time out on a write that did land) after recording an
    operation and before recording its result. The pointer keeps that operation's key in `pending`
    until it is settled. Settling reconciles with the target ref and never writes it, because the
    verdicts the operation was planned on may be stale (its commit may have turned red since):

    - the ref names the operation's target: the write landed, so the operation is applied;
    - the ref is where the pointer is (or the write could not have happened, with no identity):
      it did not land, so the operation is abandoned and the caller plans again from fresh verdicts;
    - anything else: someone else moved the ref; it stays pending and is reported every run.
    """
    at = at or now_iso()
    cur = store.pointer(repo, ref)
    if not cur.pending:
        return None
    op = store.op(cur.pending)
    if op is None:
        raise ReleaseError(f"{repo} {ref} waits on operation {cur.pending}, which is not in the store")
    # Reads need no identity (the repos are public), so a write an earlier run made with a token is
    # found even by a run without one.
    now_at = mirror.read_ref(repo, ref)
    landed = now_at == op.to_commit
    if not landed and mirror.can_write() and now_at not in _expected(cur, op):
        raise ReleaseError(f"{repo}: {ref} is at {now_at[:12] or '(absent)'}, which neither the pointer "
                           f"nor its pending operation {op.key[:12]} names; something else moved it")
    if landed:
        op.mirror, op.error = "already there", ""
        return _applied(store, cur, op, at)
    op.state, op.error = "abandoned", "did not land; planned again from fresh verdicts"
    store.save({store.op_path(op.key): op.to_json(),
                store.pointer_path(repo, ref): Pointer.from_dict({**cur.to_dict(), "pending": ""}).to_json()},
               f"abandon {op.kind} {repo} {ref} ({op.key[:12]})")
    return op


def _expected(cur: Pointer, op: Operation) -> list[str]:
    """Where the target ref may be before the write: where the pointer is. While the ref has not
    been written since the pointer last moved (no executor identity yet), also absent or at any
    earlier value, since only the executor writes it."""
    expected = [op.from_commit]
    if not cur.mirrored:
        expected += [""] + [h["commit"] for h in cur.history]
    return expected


def _applied(store: Store, cur: Pointer, op: Operation, at: str, derived=None) -> Operation:
    new = cur.moved(op, at, mirrored=op.mirror in MIRRORED)
    op.state, op.applied_at = "applied", at
    files = {store.op_path(op.key): op.to_json(), store.pointer_path(op.repo, op.ref): new.to_json()}
    if derived is not None:
        files.update(derived(new))
    store.save(files,
               f"{op.kind} {op.repo} {op.ref} {op.from_commit[:12] or '(new)'} -> {op.to_commit[:12]} "
               f"({op.key[:12]})")
    return op


def move(store: Store, mirror, op: Operation, at: str | None = None,
         derived=None) -> tuple[Operation, Pointer]:
    """Apply `op`. `mirror` is a backend with write_ref(repo, ref, expected, new) -> str. `derived`,
    if given, maps the new pointer to more files (path -> text) recorded in the same commit."""
    at = at or now_iso()
    cur = store.pointer(op.repo, op.ref)
    done = store.op(op.key)
    if done is not None and done.state == "applied":
        return done, cur
    if cur.pending:
        raise ReleaseError(f"{op.repo} {op.ref} waits on operation {cur.pending}; settle it first "
                           "(executor.finish_pending)")
    if (cur.commit, cur.generation) != (op.from_commit, op.generation):
        raise ReleaseError(f"{op.repo} {op.ref} moved since this operation was planned "
                           f"(now {cur.commit[:12] or 'unset'} generation {cur.generation}); plan it again")
    if not op.to_commit:
        raise ReleaseError(f"{op.repo} {op.ref}: an operation must name a commit")

    # 1. The key is recorded, and published, before anything outside the store changes. The pointer
    #    says it waits on this operation until the result is recorded. (An abandoned or failed
    #    earlier attempt with the same key is recorded afresh.)
    op.state, op.recorded_at, op.error, op.applied_at = "recorded", at, "", ""
    waiting = Pointer.from_dict({**cur.to_dict(), "pending": op.key})
    store.save({store.op_path(op.key): op.to_json(), store.pointer_path(op.repo, op.ref): waiting.to_json()},
               f"record {op.kind} {op.repo} {op.ref} -> {op.to_commit[:12]} ({op.key[:12]})")

    # 2. The effect: the target repo's ref, moved only from where it should be.
    try:
        op.mirror = mirror.write_ref(op.repo, op.ref, _expected(cur, op), op.to_commit)
    except ReleaseError as e:
        # The write may still have landed (a timeout): the pointer stays pending, and the next run
        # settles it against the ref.
        op.state, op.error = "failed", str(e)
        try:
            store.save({store.op_path(op.key): op.to_json()},
                       f"failed {op.kind} {op.repo} {op.ref} ({op.key[:12]})")
        except ReleaseError as save_error:
            raise ReleaseError(f"{e} (and recording the failure failed too: {save_error})") from None
        raise

    # 3. The new pointer and the applied operation, in one commit.
    op = _applied(store, cur, op, at, derived)
    return op, store.pointer(op.repo, op.ref)
