"""V0-REL-02: channel pointers and rollback.

A channel is a pointer, not a build: `channels/<name>` names a commit and an artifact digest. Rules
come from infra-config's `channels.toml` (which channels exist, in order, and where each takes its
build from) and `repos.toml` (which channels a repo ships on). Every move is an executor operation
(`executor.move`), so its key is recorded before the ref changes.

- `promote` points a channel at the commit its source names now (canary takes lkgr's commit) with
  the digest of the artifact built from it.
- `rollback` points it back at its previous value, from the pointer's history. Nothing is rebuilt.

After each move the published manifest `channels.json` says what every channel names; the installer
reads it (V0-INS-01).
"""
from __future__ import annotations

import json
import re

from qqrelease import executor
from qqrelease.errors import ReleaseError
from qqrelease.operations import Operation, Pointer
from qqrelease.store import Store

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST = "channels.json"
MANIFEST_SCHEMA = "qq-channels/1"


def ref_of(channel: str) -> str:
    return f"channels/{channel}"


def channel_cfg(cfg: dict, name: str) -> dict:
    for c in cfg.get("channels", {}).get("channel", []):
        if c.get("name") == name:
            return c
    raise ReleaseError(f"channels.toml has no channel {name!r}")


def check_repo(cfg: dict, repo: str, channel: str) -> None:
    for r in cfg.get("repos", {}).get("repo", []):
        if r.get("name") == repo:
            if channel not in r.get("channels", []):
                raise ReleaseError(f"{repo} does not ship on {channel} (repos.toml channels)")
            return
    raise ReleaseError(f"{repo} is not onboarded in infra-config repos.toml")


def source_ref(cfg: dict, channel: str) -> str:
    """Where a channel takes its build from: lkgr's ref, or the channel before it."""
    src = channel_cfg(cfg, channel).get("from", "")
    lkgr = cfg.get("channels", {}).get("source", {}).get("ref", "")
    if not src:
        raise ReleaseError(f"channels.toml: channel {channel!r} has no `from`")
    return src if src == lkgr else ref_of(src)


def plan_promote(cfg: dict, store: Store, repo: str, channel: str, commit: str, digest: str,
                 reason: str = "", actor: str = "local") -> Operation:
    check_repo(cfg, repo, channel)
    if not DIGEST.match(digest):
        raise ReleaseError(f"digest {digest!r} is not sha256:<64 hex>; a channel names an artifact, "
                           "not just a commit")
    src = source_ref(cfg, channel)
    at = store.pointer(repo, src)
    if not at.commit:
        raise ReleaseError(f"{repo} has no {src} yet, so there is nothing to promote to {channel}")
    if commit != at.commit:
        raise ReleaseError(f"{repo} {channel} takes its build from {src}, which names {at.commit[:12]}, "
                           f"not {commit[:12]}")
    return executor.plan(store, "promote", repo, ref_of(channel), commit, digest=digest,
                         reason=reason or f"promote {src} {commit[:12]} to {channel}", actor=actor)


def plan_rollback(cfg: dict, store: Store, repo: str, channel: str, reason: str = "",
                  actor: str = "local") -> Operation:
    check_repo(cfg, repo, channel)
    cur = store.pointer(repo, ref_of(channel))
    if not cur.commit:
        raise ReleaseError(f"{repo} {channel} names nothing yet: there is nothing to roll back")
    if not cur.history:
        raise ReleaseError(f"{repo} {channel} has no previous value to roll back to")
    prev = cur.history[0]
    return executor.plan(store, "rollback", repo, ref_of(channel), prev["commit"], digest=prev["digest"],
                         reason=reason or f"roll {channel} back from {cur.commit[:12]} to {prev['commit'][:12]}",
                         actor=actor)


def apply(store: Store, mirror, op: Operation, at: str | None = None) -> tuple[Operation, Pointer]:
    return executor.move(store, mirror, op, at=at,
                         derived=lambda new: {store.root / MANIFEST: manifest_json(store, new)})


def manifest(store: Store, new: Pointer | None = None) -> dict:
    """What every channel names, with `new` in place of its stored value."""
    pointers = {(p.repo, p.ref): p for p in store.pointers()}
    if new is not None:
        pointers[(new.repo, new.ref)] = new
    repos: dict[str, dict] = {}
    for p in sorted(pointers.values(), key=lambda p: (p.repo, p.ref)):
        if not p.ref.startswith("channels/") or not p.commit:
            continue
        repos.setdefault(p.repo, {})[p.ref.removeprefix("channels/")] = {
            "commit": p.commit, "digest": p.digest, "generation": p.generation, "op": p.op,
            "updated_at": p.updated_at}
    return {"schema": MANIFEST_SCHEMA, "repos": repos}


def manifest_json(store: Store, new: Pointer | None = None) -> str:
    return json.dumps(manifest(store, new), sort_keys=True, indent=2) + "\n"
