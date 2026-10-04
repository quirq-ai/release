"""GitHub backend: a pointer's ref is the branch `refs/heads/<ref>` in the target repo, moved through
the REST API.

Only the release executor identity may write `lkgr` and `channels/**` (gate's `qq-release-refs`
rulesets). Its installation token comes from `QQ_RELEASE_TOKEN`. TODO(suraj): the executor's
identity (a GitHub App) does not exist yet; until it does, every write is skipped and says so, and
the pointer still moves in the state store, which is the record readers use.

The write compares before it swaps: if the ref is not at `old` (and not already at `new`), it
refuses. The concurrency group serializes executor runs, so the gap between read and write has
no other writer.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from qqrelease.errors import ReleaseError

API = "https://api.github.com"
NO_IDENTITY = "skipped: no release executor identity (QQ_RELEASE_TOKEN unset; TODO(suraj))"


class Mirror:
    def __init__(self, repos: dict[str, str] | None = None, token: str | None = None, **_):
        self.slugs = repos or {}          # infra-config name -> "<owner>/<repo>"
        self.token = token if token is not None else os.environ.get("QQ_RELEASE_TOKEN", "")

    def _slug(self, repo: str) -> str:
        slug = self.slugs.get(repo, "")
        if not slug:
            raise ReleaseError(f"{repo}: no github.com source in infra-config repos.toml")
        return slug

    def read_ref(self, repo: str, ref: str) -> str:
        doc = self._request("GET", f"/repos/{self._slug(repo)}/git/ref/heads/{_quote(ref)}")
        return (doc or {}).get("object", {}).get("sha", "")

    def write_ref(self, repo: str, ref: str, old: str, new: str) -> str:
        if not self.token:
            return NO_IDENTITY
        cur = self.read_ref(repo, ref)
        if cur == new:
            return "already there"
        if cur != old:
            raise ReleaseError(f"{repo}: {ref} is at {cur[:12] or '(absent)'}, not {old[:12] or '(absent)'}; "
                               "something other than the executor moved it")
        slug = self._slug(repo)
        if cur:
            self._request("PATCH", f"/repos/{slug}/git/refs/heads/{_quote(ref)}", {"sha": new, "force": True})
        else:
            self._request("POST", f"/repos/{slug}/git/refs", {"ref": f"refs/heads/{ref}", "sha": new})
        return "pushed"

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            raise ReleaseError(f"GitHub API {method} {path}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ReleaseError(f"GitHub API {method} {path}: {e}") from None


def _quote(ref: str) -> str:
    return urllib.parse.quote(ref, safe="/")
