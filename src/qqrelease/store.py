"""The release state store: the record of every pointer and every operation.

It is a directory, in CI a git worktree of this repo's `release-state` branch:

    pointers/<repo>/<ref>.json   what `lkgr` and each `channels/<name>` name (schema qq-pointer/1)
    ops/<key>.json               every operation, recorded before its effect (schema qq-operation/1)

`save` writes files and, in a git worktree, commits them; with `push` it also pushes the branch
before returning, so an operation key is published before the effect it guards. The branch history
is the audit log. Writers are serialized by the workflows' shared concurrency group; a rejected push
(someone else wrote first) is an error, never retried blindly.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from qqrelease.errors import ReleaseError
from qqrelease.operations import Operation, Pointer

BRANCH = "release-state"


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
        self._git("-c", "user.name=qq-release", "-c", "user.email=qq-release@quirq.invalid",
                  "commit", "-q", "-m", message)
        if self.push:
            p = self._git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", check=False)
            if p.returncode != 0:
                raise ReleaseError(f"could not publish to {self.branch} (another writer?): {p.stderr.strip()}")

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
