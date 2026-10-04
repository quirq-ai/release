"""The release state store: the record of every pointer and every operation.

It is a directory, in CI a git worktree of this repo's `release-state` branch:

    pointers/<repo>/<ref>.json   what `lkgr` and each `channels/<name>` name (schema qq-pointer/1)
    ops/<key>.json               every operation, recorded before its effect (schema qq-operation/1)

`save` writes files and, in a git worktree, commits them; with `push` it also pushes the branch
before returning, so an operation key is published before the effect it guards. The branch history
is the audit log. Writers of the same files are serialized by concurrency groups (`lkgr` writes
lkgr pointers; the canary and rollback workflows write channel pointers, channels.json and canary
records), so two writers never touch the same file. A push that loses a race to the other group is
rebased onto it and pushed again; a rebase that conflicts is an error, never resolved blindly.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from qqrelease.errors import ReleaseError
from qqrelease.operations import Operation, Pointer

BRANCH = "release-state"
IDENTITY = ("-c", "user.name=qq-release", "-c", "user.email=qq-release@quirq.invalid")


class Store:
    def __init__(self, root: str | Path, push: bool = False, branch: str = BRANCH):
        self.root = Path(root)
        self.push = push
        self.branch = branch
        self.root.mkdir(parents=True, exist_ok=True)
        self.is_git = (self.root / ".git").exists()
        if push and not self.is_git:
            raise ReleaseError(f"{self.root} is not a git worktree, so there is nothing to push")

    # --- reads --------------------------------------------------------------------------------

    def pointer_path(self, repo: str, ref: str) -> Path:
        _check_name(repo)
        for part in ref.split("/"):
            _check_name(part)
        return self.root / "pointers" / repo / f"{ref}.json"

    def pointer(self, repo: str, ref: str) -> Pointer:
        p = self.pointer_path(repo, ref)
        if not p.is_file():
            return Pointer(repo=repo, ref=ref)
        return Pointer.from_dict(_read_json(p))

    def op_path(self, key: str) -> Path:
        _check_name(key)
        return self.root / "ops" / f"{key}.json"

    def op(self, key: str) -> Operation | None:
        p = self.op_path(key)
        return Operation.from_dict(_read_json(p)) if p.is_file() else None

    def pointers(self) -> list[Pointer]:
        return [Pointer.from_dict(_read_json(p))
                for p in sorted((self.root / "pointers").rglob("*.json"))]

    # --- writes -------------------------------------------------------------------------------

    def save(self, files: dict[Path, str], message: str) -> None:
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if not self.is_git:
            return
        self._git("add", "--", *[str(p.relative_to(self.root)) for p in files])
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self._git(*IDENTITY, "commit", "-q", "-m", message)
        if self.push:
            self._publish()

    def _publish(self, attempts: int = 4) -> None:
        err = ""
        for _ in range(attempts):
            p = self._git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", check=False)
            if p.returncode == 0:
                return
            err = p.stderr.strip()
            # Lost a race with the other writer group: replay our commit on top of theirs.
            r = self._git(*IDENTITY, "pull", "-q", "--rebase", "origin", self.branch, check=False)
            if r.returncode != 0:
                self._git("rebase", "--abort", check=False)
                raise ReleaseError(f"could not publish to {self.branch}: rebasing onto another writer's "
                                   f"change conflicted: {r.stderr.strip()}")
        raise ReleaseError(f"could not publish to {self.branch} after {attempts} attempts: {err}")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise ReleaseError(f"git {' '.join(args)} failed in {self.root}: {p.stderr.strip()}")
        return p


def _check_name(part: str) -> None:
    if not part or part in (".", "..") or "/" in part or "\\" in part:
        raise ReleaseError(f"bad name in the state store: {part!r}")


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise ReleaseError(f"{p}: unreadable state: {e}") from None
