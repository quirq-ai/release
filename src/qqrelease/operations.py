"""Operations: every external effect is keyed and recorded before it happens (plan §2 rule 5, §5.4).

An operation moves one pointer (`lkgr` or `channels/<name>`) of one repo from one commit to another.
Its key hashes what it intends plus the pointer's generation, so:

- a retry of the same intent (same pointer state) has the same key and never acts twice;
- moving forward again to a commit the pointer once held (after a rollback) is a new operation.

The life of an operation: `recorded` (the key is published to the state store, nothing changed
yet), then the effect, then `applied` with the pointer, or `failed` with the reason.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "qq-operation/1"


@dataclass
class Operation:
    kind: str                # "advance" (lkgr), "promote" (a channel), "rollback"
    repo: str
    ref: str                 # "lkgr", "channels/canary"
    from_commit: str         # "" when the pointer does not exist yet
    to_commit: str
    generation: int          # the pointer's generation this operation starts from
    digest: str = ""         # artifact digest the pointer names; "" for lkgr
    reason: str = ""
    actor: str = ""          # who ran the executor: a workflow run URL, or "local"
    state: str = "recorded"  # recorded, applied, failed
    recorded_at: str = ""
    applied_at: str = ""
    mirror: str = ""         # what happened to the target repo's git ref: "pushed", "already there",
                             # or "skipped: <why>"
    error: str = ""
    key: str = ""
    schema: str = SCHEMA

    def __post_init__(self):
        if not self.key:
            self.key = key_of(self)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Operation":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def key_of(op: Operation) -> str:
    intent = {"kind": op.kind, "repo": op.repo, "ref": op.ref, "from": op.from_commit,
              "to": op.to_commit, "digest": op.digest, "generation": op.generation}
    blob = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"qq-op/1\n" + blob).hexdigest()[:32]


@dataclass
class Pointer:
    """What a ref names: a commit, plus an artifact digest for channels. `history` holds earlier
    values, newest first, so rollback restores one without rebuilding anything."""
    repo: str
    ref: str
    commit: str = ""
    digest: str = ""
    generation: int = 0
    op: str = ""              # key of the operation that set it
    updated_at: str = ""
    pending: str = ""         # key of an operation recorded but not yet applied
    mirrored: bool = False    # whether the target repo's git ref is known to name `commit`
    history: list[dict[str, Any]] = field(default_factory=list)
    schema: str = "qq-pointer/1"

    HISTORY = 20

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Pointer":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def moved(self, op: Operation, at: str, mirrored: bool) -> "Pointer":
        prev = ([{"commit": self.commit, "digest": self.digest, "generation": self.generation,
                  "op": self.op, "updated_at": self.updated_at}] if self.commit else [])
        return Pointer(repo=self.repo, ref=self.ref, commit=op.to_commit, digest=op.digest,
                       generation=self.generation + 1, op=op.key, updated_at=at, mirrored=mirrored,
                       history=(prev + self.history)[:self.HISTORY])
