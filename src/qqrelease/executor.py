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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def plan(store: Store, kind: str, repo: str, ref: str, to_commit: str, digest: str = "",
         reason: str = "", actor: str = "local") -> Operation:
    cur = store.pointer(repo, ref)
    return Operation(kind=kind, repo=repo, ref=ref, from_commit=cur.commit, to_commit=to_commit,
                     generation=cur.generation, digest=digest, reason=reason, actor=actor)


def move(store: Store, mirror, op: Operation, at: str | None = None) -> tuple[Operation, Pointer]:
    """Apply `op`. `mirror` is a backend with write_ref(repo, ref, old, new) -> str."""
    at = at or now_iso()
    cur = store.pointer(op.repo, op.ref)
    done = store.op(op.key)
    if done is not None and done.state == "applied":
        return done, cur
    if (cur.commit, cur.generation) != (op.from_commit, op.generation):
        raise ReleaseError(f"{op.repo} {op.ref} moved since this operation was planned "
                           f"(now {cur.commit[:12] or 'unset'} generation {cur.generation}); plan it again")
    if not op.to_commit:
        raise ReleaseError(f"{op.repo} {op.ref}: an operation must name a commit")

    # 1. The key is recorded, and published, before anything outside the store changes.
    if done is None:
        op.state, op.recorded_at = "recorded", at
        store.save({store.op_path(op.key): op.to_json()},
                   f"record {op.kind} {op.repo} {op.ref} -> {op.to_commit[:12]} ({op.key[:12]})")
    else:
        op = done

    # 2. The effect: the target repo's ref.
    try:
        op.mirror = mirror.write_ref(op.repo, op.ref, op.from_commit, op.to_commit)
    except ReleaseError as e:
        op.state, op.error = "failed", str(e)
        store.save({store.op_path(op.key): op.to_json()},
                   f"failed {op.kind} {op.repo} {op.ref} ({op.key[:12]})")
        raise

    # 3. The new pointer and the applied operation, in one commit.
    new = cur.moved(op, at)
    op.state, op.applied_at = "applied", at
    store.save({store.op_path(op.key): op.to_json(), store.pointer_path(op.repo, op.ref): new.to_json()},
               f"{op.kind} {op.repo} {op.ref} {op.from_commit[:12] or '(new)'} -> {op.to_commit[:12]} "
               f"({op.key[:12]})")
    return op, new
