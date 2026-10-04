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
    """Finish the operation a pointer is waiting on, if any, before planning a new one.

    A run can die (or lose its push, or time out on a write that did land) after recording an
    operation and before recording its result. The pointer keeps that operation's key in `pending`
    until it is applied, so the next run re-applies it: the backend sees the ref already at the
    target, or moves it there. Only then does the pointer move on. An operation that still fails
    (the ref is somewhere neither end of it names) stays pending and is reported every run.
    """
    cur = store.pointer(repo, ref)
    if not cur.pending:
        return None
    op = store.op(cur.pending)
    if op is None:
        raise ReleaseError(f"{repo} {ref} waits on operation {cur.pending}, which is not in the store")
    done, _ = move(store, mirror, op, at=at)
    return done


def move(store: Store, mirror, op: Operation, at: str | None = None,
         derived=None) -> tuple[Operation, Pointer]:
    """Apply `op`. `mirror` is a backend with write_ref(repo, ref, expected, new) -> str. `derived`,
    if given, maps the new pointer to more files (path -> text) recorded in the same commit."""
    at = at or now_iso()
    cur = store.pointer(op.repo, op.ref)
    done = store.op(op.key)
    if done is not None and done.state == "applied":
        return done, cur
    if cur.pending and cur.pending != op.key:
        raise ReleaseError(f"{op.repo} {op.ref} waits on operation {cur.pending}; finish it first")
    if (cur.commit, cur.generation) != (op.from_commit, op.generation):
        raise ReleaseError(f"{op.repo} {op.ref} moved since this operation was planned "
                           f"(now {cur.commit[:12] or 'unset'} generation {cur.generation}); plan it again")
    if not op.to_commit:
        raise ReleaseError(f"{op.repo} {op.ref}: an operation must name a commit")

    # 1. The key is recorded, and published, before anything outside the store changes. The pointer
    #    says it waits on this operation until the result is recorded.
    if done is None:
        op.state, op.recorded_at = "recorded", at
        waiting = Pointer.from_dict({**cur.to_dict(), "pending": op.key})
        store.save({store.op_path(op.key): op.to_json(), store.pointer_path(op.repo, op.ref): waiting.to_json()},
                   f"record {op.kind} {op.repo} {op.ref} -> {op.to_commit[:12]} ({op.key[:12]})")
    else:
        op = done      # recorded or failed by an earlier run: apply it again
        op.error = ""

    # 2. The effect: the target repo's ref. Where it may be now: where the pointer was. And while the
    #    ref has not been written since the pointer last moved (no executor identity yet), absent or
    #    at any earlier value, since only the executor writes it.
    expected = [op.from_commit]
    if not cur.mirrored:
        expected += [""] + [h["commit"] for h in cur.history]
    try:
        op.mirror = mirror.write_ref(op.repo, op.ref, expected, op.to_commit)
    except ReleaseError as e:
        # The write may still have landed (a timeout): the pointer stays pending, and the next run
        # finds out by applying the operation again.
        op.state, op.error = "failed", str(e)
        try:
            store.save({store.op_path(op.key): op.to_json()},
                       f"failed {op.kind} {op.repo} {op.ref} ({op.key[:12]})")
        except ReleaseError as save_error:
            raise ReleaseError(f"{e} (and recording the failure failed too: {save_error})") from None
        raise

    # 3. The new pointer and the applied operation, in one commit.
    new = cur.moved(op, at, mirrored=op.mirror in MIRRORED)
    op.state, op.applied_at = "applied", at
    files = {store.op_path(op.key): op.to_json(), store.pointer_path(op.repo, op.ref): new.to_json()}
    if derived is not None:
        files.update(derived(new))
    store.save(files,
               f"{op.kind} {op.repo} {op.ref} {op.from_commit[:12] or '(new)'} -> {op.to_commit[:12]} "
               f"({op.key[:12]})")
    return op, new
