"""Target repos as local git directories: `<root>/<repo>`. Refs move with `git update-ref`, which
compares and swaps, exactly as the forge's ref update should."""
from __future__ import annotations

import subprocess
from pathlib import Path

from qqrelease.errors import ReleaseError

ZERO = "0" * 40


class Mirror:
    def __init__(self, target_root: str | Path | None = None, **_):
        if not target_root:
            raise ReleaseError("the local backend needs --target-root (a directory of git repos)")
        self.root = Path(target_root)

    def _repo(self, repo: str) -> Path:
        d = self.root / repo
        if not (d / ".git").exists() and not (d / "HEAD").is_file():
            raise ReleaseError(f"{d} is not a git repository")
        return d

    def read_ref(self, repo: str, ref: str) -> str:
        p = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}"],
                           cwd=self._repo(repo), capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""

    def actor(self) -> str:
        return "local"

    def write_ref(self, repo: str, ref: str, expected: list[str], new: str) -> str:
        cur = self.read_ref(repo, ref)
        if cur == new:
            return "already there"
        if cur not in expected:
            raise ReleaseError(f"{repo}: {ref} is at {cur[:12] or '(absent)'}, which the executor did not "
                               "set; something else moved it")
        # update-ref compares and swaps against what we read.
        p = subprocess.run(["git", "update-ref", f"refs/heads/{ref}", new, cur or ZERO],
                           cwd=self._repo(repo), capture_output=True, text=True)
        if p.returncode != 0:
            raise ReleaseError(f"{repo}: could not move {ref} {cur[:12] or '(new)'} -> {new[:12]}: "
                               f"{p.stderr.strip()}")
        return "pushed"
