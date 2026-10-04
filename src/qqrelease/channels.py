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

# channels.json is read by the installer, which refuses the whole file on one bad entry, so every
# value written must pass the same rules (installer's qqinstall.manifest NAME, COMMIT, DIGEST).
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
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
    for what, name in (("repo", repo), ("channel", channel)):
        if not NAME.fullmatch(name):
            raise ReleaseError(f"{what} name {name!r} is not one the installer accepts "
                               "([A-Za-z0-9][A-Za-z0-9._-]{0,99})")
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


def check_automatic(cfg: dict, channel: str) -> None:
    """v0 moves only channels whose rules need no person and no signal it cannot read yet: an
    approval, a soak or health signals mean dev (v1) and stable (v2) machinery, which does not exist.
    A missing signal holds, so those promotions are refused rather than done without the check."""
    c = channel_cfg(cfg, channel)
    rules = c.get("promotion", {})
    needs = []
    if rules.get("approval", "none") != "none":
        needs.append(f"approval = {rules['approval']!r}")
    if rules.get("min_soak_hours", 0):
        needs.append(f"min_soak_hours = {rules['min_soak_hours']}")
    if rules.get("health_signals"):
        needs.append("health_signals = " + ", ".join(rules["health_signals"]))
    if needs:
        raise ReleaseError(f"{channel} promotion needs {'; '.join(needs)} (channels.toml), which v0 cannot "
                           "check; it is held, not promoted")


def plan_promote(cfg: dict, store: Store, repo: str, channel: str, commit: str, digest: str,
                 reason: str = "", actor: str = "local") -> Operation:
    check_repo(cfg, repo, channel)
    check_automatic(cfg, channel)
    if not COMMIT.fullmatch(commit):
        raise ReleaseError(f"commit {commit!r} is not a 40-character lowercase hex SHA")
    if not DIGEST.fullmatch(digest):
        raise ReleaseError(f"digest {digest!r} is not sha256:<64 hex>; a channel names an artifact, "
                           "not just a commit")
    src = source_ref(cfg, channel)
    at = store.pointer(repo, src)
    if not at.commit:
        raise ReleaseError(f"{repo} has no {src} yet, so there is nothing to promote to {channel}")
    if not named_since(store, at, commit):
        raise ReleaseError(f"{repo} {channel} takes its build from {src}, which names {at.commit[:12]}; "
                           f"{commit[:12]} is not it, nor an earlier value it moved forward from")
    if src.startswith("channels/") and digest != at.digest:
        raise ReleaseError(f"{repo} {channel} takes its artifact from {src}, which names {at.digest}, "
                           f"not {digest}: only an artifact the channel before vetted moves on")
    cur = store.pointer(repo, ref_of(channel))
    if (cur.commit, cur.digest) == (commit, digest):
        raise ReleaseError(f"{repo} {channel} already names {commit[:12]} {digest}")
    if any(b["commit"] == commit for b in cur.rolled_back):
        raise ReleaseError(f"{repo} {channel} was rolled back from {commit[:12]}; it is not shipped again")
    return executor.plan(store, "promote", repo, ref_of(channel), commit, digest=digest,
                         reason=reason or f"promote {src} {commit[:12]} to {channel}", actor=actor)


def rollback_target(cur: Pointer) -> dict | None:
    """The newest earlier value that differs from the current one and was never rolled back from."""
    bad = {(b["commit"], b["digest"]) for b in cur.rolled_back} | {(cur.commit, cur.digest)}
    return next((h for h in cur.history if (h["commit"], h["digest"]) not in bad), None)


def named_since(store: Store, ptr: Pointer, commit: str) -> bool:
    """Whether `ptr` names `commit` now, or named it earlier and has only moved forward since.

    The canary builds what lkgr named when it started; lkgr may advance during the build, which
    leaves that commit good. But if lkgr retreated past it (a re-run turned a commit red), it is not
    known good any more.
    """
    if ptr.commit == commit:
        return True
    ops_since = [ptr.op]
    for h in ptr.history:
        if h["commit"] == commit:
            return all((op := store.op(k)) is not None and op.kind != "retreat" for k in ops_since)
        ops_since.append(h["op"])
    return False


def plan_rollback(cfg: dict, store: Store, repo: str, channel: str, reason: str = "",
                  actor: str = "local", from_commit: str = "") -> Operation:
    check_repo(cfg, repo, channel)
    rules = channel_cfg(cfg, channel).get("rollback", {})
    if rules.get("approval", "none") != "none":
        raise ReleaseError(f"rolling {channel} back needs approval = {rules['approval']!r} (channels.toml), "
                           "which v0 cannot check")
    cur = store.pointer(repo, ref_of(channel))
    if not cur.commit:
        raise ReleaseError(f"{repo} {channel} names nothing yet: there is nothing to roll back")
    if from_commit and not cur.commit.startswith(from_commit):
        raise ReleaseError(f"{repo} {channel} names {cur.commit[:12]}, not {from_commit[:12]}: it has moved "
                           "(or this rollback already ran), so not rolling back")
    prev = rollback_target(cur)
    if prev is None:
        raise ReleaseError(f"{repo} {channel} has no previous value to roll back to")
    if not COMMIT.fullmatch(str(prev["commit"])) or not DIGEST.fullmatch(str(prev["digest"])):
        # Checked before the ref moves, not after: history written before these rules existed.
        raise ReleaseError(f"{repo} {channel}'s previous value {prev['commit']!r} {prev['digest']!r} is "
                           "one the installer refuses; not rolling back to it")
    return executor.plan(store, "rollback", repo, ref_of(channel), prev["commit"], digest=prev["digest"],
                         reason=reason or f"roll {channel} back from {cur.commit[:12]} to {prev['commit'][:12]}",
                         actor=actor)


def derived(store: Store):
    """channels.json, recorded in the same commit as every channel move."""
    return lambda new: {store.root / MANIFEST: manifest_json(store, new)}


def settle(store: Store, mirror, repo: str, channel: str, at: str | None = None) -> Operation | None:
    return executor.finish_pending(store, mirror, repo, ref_of(channel), at=at, derived=derived(store))


def apply(store: Store, mirror, op: Operation, at: str | None = None) -> tuple[Operation, Pointer]:
    return executor.move(store, mirror, op, at=at, derived=derived(store))


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


def check_manifest(doc: dict) -> None:
    """Refuse to publish what the installer would refuse: one bad entry blinds every channel."""
    for repo, chans in doc["repos"].items():
        for name, e in chans.items():
            where = f"channels.json {repo} {name}"
            if not NAME.fullmatch(repo) or not NAME.fullmatch(name):
                raise ReleaseError(f"{where}: a name the installer refuses")
            if not COMMIT.fullmatch(str(e["commit"])) or not DIGEST.fullmatch(str(e["digest"])):
                raise ReleaseError(f"{where}: commit {e['commit']!r} or digest {e['digest']!r} the installer refuses")
            if not isinstance(e["generation"], int) or isinstance(e["generation"], bool) or e["generation"] < 1:
                raise ReleaseError(f"{where}: generation {e['generation']!r} the installer refuses")


def manifest_json(store: Store, new: Pointer | None = None) -> str:
    doc = manifest(store, new)
    check_manifest(doc)
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"
