"""The release state store: the record of every pointer and every operation.

It is a directory, in CI a git worktree of this repo's `release-state` branch:

    pointers/<repo>/<ref>.json   what `lkgr` and each `channels/<name>` name (schema qq-pointer/1)
    ops/<key>.json               every operation, recorded before its effect (schema qq-operation/1)

`save` writes files and, in a git worktree, commits them; with `push` it also pushes the branch
before returning, so an operation key is published before the effect it guards. The branch history
is the audit log. Writers of the same files are serialized by concurrency groups (`lkgr` writes
lkgr pointers; the canary and rollback workflows write channel pointers, channels.json and canary
records), so two writers never touch the same file. A push that loses a race to the other group is
rebased onto it and pushed again, but only when the other writer touched none of its files; anything
else is an error, never resolved blindly, and a failed publish resets the worktree to the branch.
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
        if not self.push:
            return
        try:
            self._publish()
        except Exception:
            # S7: a refused commit must not ride along with the next save (another repo's record),
            # so the worktree goes back to what the branch holds now.
            self._reset_to_branch()
            raise

    def _reset_to_branch(self) -> None:
        self._git("rebase", "--abort", check=False)
        f = self._git("fetch", "-q", "origin", f"refs/heads/{self.branch}", check=False)
        self._git("reset", "-q", "--hard", "FETCH_HEAD" if f.returncode == 0 else "HEAD~1", check=False)

    def _publish(self, attempts: int = 4) -> None:
        err = ""
        for _ in range(attempts):
            p = self._git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", check=False)
            if p.returncode == 0:
                return
            err = p.stderr.strip()
            # Lost a race with another writer: replay our commit on top of theirs, but only if they
            # touched none of our files (a compare-and-swap per file; a textual merge could combine
            # two records that each made sense alone into one that does not).
            self._git("fetch", "-q", "origin", f"refs/heads/{self.branch}")
            base = self._git("rev-parse", "-q", "--verify", "HEAD~1^{commit}", check=False).stdout.strip()
            if not base:      # our commit is the branch's first: compare with the empty tree
                base = self._git("hash-object", "-t", "tree", "/dev/null").stdout.strip()
            ours = set(self._git("diff", "--name-only", base, "HEAD").stdout.split())
            theirs = set(self._git("diff", "--name-only", base, "FETCH_HEAD").stdout.split())
            if ours & theirs:
                raise ReleaseError(f"could not publish to {self.branch}: another writer changed "
                                   f"{', '.join(sorted(ours & theirs))} meanwhile (a conflict); run again")
            r = self._git(*IDENTITY, "rebase", "-q", "FETCH_HEAD", check=False)
            if r.returncode != 0:
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
