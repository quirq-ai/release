"""The executor action's release-state push credential (push-credential.sh and its check), run against
the git layout actions/checkout v7.0.1 leaves: a credentials file under $RUNNER_TEMP that .git/config
includes through `includeIf.gitdir` for the repository and for every linked worktree. store.py
pushes from the linked worktree .qq/state, so the header that counts is the one seen from there."""
import base64
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "executor"
KEY = "http.https://github.com/.extraheader"
ORIGIN = "https://github.com/quirq-ai/release"


def header(token: str) -> str:
    return "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A repository with a linked worktree at .qq/state, outside any user or system git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    repo, temp = tmp_path / "work", tmp_path / "runner-temp"
    repo.mkdir()
    temp.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
    git("remote", "add", "origin", ORIGIN)
    git("worktree", "add", "-q", "--orphan", "-b", "release-state", ".qq/state")
    return repo, temp, git


def checkout_persisted_credentials(repo: Path, temp: Path, git, token: str = "checkout-token"):
    """What checkout v7.0.1 does with persist-credentials left on (src/git-auth-helper.ts)."""
    creds = temp / "git-credentials-0000.config"
    creds.write_text(f'[http "https://github.com/"]\n\textraheader = {header(token)}\n')
    git("config", "--local", f"includeIf.gitdir:{repo}/.git.path", str(creds))
    git("config", "--local", f"includeIf.gitdir:{repo}/.git/worktrees/*.path", str(creds))


def install(repo: Path, temp: Path, **env):
    out = temp / "github-output"
    out.write_text("")
    full = {**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(out),
            "CLIENT_ID": "", "APP_TOKEN": "", "JOB_TOKEN": "", **env}
    p = subprocess.run(["bash", str(ACTION / "push-credential.sh")], cwd=repo, env=full,
                       capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return p, outputs


def check(state: Path, expected: str, slug: str = ""):
    env = {**os.environ, "EXPECTED_SHA256": expected, "APP_SLUG": slug,
           "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "quirq-ai/release"}
    return subprocess.run(["bash", str(ACTION / "push-credential-check.sh")], cwd=state, env=env,
                          capture_output=True, text=True)


def test_the_checkout_layout_reaches_the_worktree(layout):
    """The premise: without persist-credentials: false, the checkout's header is what .qq/state sends."""
    repo, temp, git = layout
    checkout_persisted_credentials(repo, temp, git)
    assert git("config", "--get-all", KEY, cwd=repo / ".qq/state").strip() == header("checkout-token")


def test_app_token_is_the_one_header_seen_from_the_worktree(layout):
    repo, temp, git = layout
    p, out = install(repo, temp, CLIENT_ID="Iv1.client", APP_TOKEN="app-token", JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    state = repo / ".qq/state"
    assert git("config", "--get-all", KEY, cwd=state).splitlines() == [header("app-token")]
    assert out["header-sha256"] == hashlib.sha256(header("app-token").encode()).hexdigest()
    # Masked before it is written, and never printed otherwise.
    b64 = header("app-token").split()[-1]
    assert p.stdout.splitlines() == [f"::add-mask::{b64}"]
    assert "app-token" not in p.stdout + p.stderr
    # In a 0600 file under RUNNER_TEMP that only the local config includes: no global, no URL.
    [f] = temp.glob("qq-push-credential-*")
    assert f.stat().st_mode & 0o777 == 0o600
    assert git("config", "--local", "--get-all", "include.path").split() == [str(f)]
    assert git("remote", "get-url", "--push", "origin").strip() == ORIGIN   # no credentials in the URL
    c = check(state, out["header-sha256"], "quirq-release-executor")
    assert c.returncode == 0, c.stdout + c.stderr
    assert c.stdout.strip() == "release-state pushes as quirq-release-executor[bot]"


def test_without_client_id_the_job_token_pushes_and_says_so(layout):
    repo, temp, git = layout
    p, out = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    state = repo / ".qq/state"
    assert git("config", "--get-all", KEY, cwd=state).splitlines() == [header("job-token")]
    c = check(state, out["header-sha256"])
    assert c.returncode == 0, c.stdout + c.stderr
    assert c.stdout.strip() == "::warning::release-state pushes as github-actions[bot] (no QQ_RELEASE_CLIENT_ID)"


def test_client_id_without_a_minted_token_fails_instead_of_falling_back(layout):
    repo, temp, git = layout
    p, _ = install(repo, temp, CLIENT_ID="Iv1.client", JOB_TOKEN="job-token")
    assert p.returncode != 0
    assert "no App token was minted" in p.stdout
    assert subprocess.run(["git", "config", "--get-all", KEY], cwd=repo).returncode == 1
    assert not list(temp.glob("qq-push-credential-*"))


def test_refuses_when_the_checkout_kept_its_credentials(layout):
    repo, temp, git = layout
    checkout_persisted_credentials(repo, temp, git)
    p, _ = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode != 0
    assert "persist-credentials: false" in p.stdout


@pytest.mark.parametrize("extra, message", [
    (("http.https://github.com/.extraheader", "AUTHORIZATION: basic b3RoZXI="), "found 2"),
    (("http.extraheader", "AUTHORIZATION: basic b3RoZXI="), "found 2"),
    (("http.https://github.com/quirq-ai/release.extraheader", "AUTHORIZATION: basic b3RoZXI="), "found 2"),
    (("url.https://x-access-token:t@github.com/.pushInsteadOf", "https://github.com/"), "insteadOf"),
    (("remote.origin.pushurl", "https://x-access-token:t@github.com/quirq-ai/release"), "push URL"),
    (("url.https://x-access-token:t@github.com/quirq-ai/.insteadOf", "https://github.com/quirq-ai/"), "insteadOf"),
    (("credential.helper", "store"), "credential helper"),
    (("credential.https://github.com.helper", "store"), "credential helper"),
    (("url.https://x-access-token:t@github.com/.insteadOf", "https://github.com/"), "insteadOf"),
    (("remote.origin.pushurl", ORIGIN, "remote.origin.pushurl", "file:///sink.git"), "push URL"),
    (("url.https://github.com/quirq-ai/release.pushInsteadOf", ORIGIN,
      "url.file:///evil.git.insteadOf", ORIGIN), "fetch URL"),
    (("remote.origin.url", "file:///evil.git", "remote.origin.pushurl", ORIGIN), "more than one URL"),
    (("remote.origin.vcs", "qqx"), "remote helper"),
])
def test_check_refuses_anything_that_could_send_another_credential(layout, extra, message):
    repo, temp, git = layout
    p, out = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    for i in range(0, len(extra), 2):
        git("config", "--local", "--add", *extra[i:i + 2])
    c = check(repo / ".qq/state", out["header-sha256"])
    assert c.returncode != 0
    assert message in c.stdout


@pytest.mark.parametrize("url, message", [
    ("https://x-access-token:t@github.com/quirq-ai/release", "credentials"),
    ("https://github.com/attacker/release", "origin is not https://github.com/quirq-ai/release"),
])
def test_check_refuses_an_origin_other_than_this_repository(layout, url, message):
    repo, temp, git = layout
    p, out = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    git("remote", "set-url", "origin", url)
    c = check(repo / ".qq/state", out["header-sha256"])
    assert c.returncode != 0
    assert message in c.stdout


def test_check_accepts_origin_with_git_suffix(layout):
    repo, temp, git = layout
    p, out = install(repo, temp, JOB_TOKEN="job-token")
    git("remote", "set-url", "origin", ORIGIN + ".git")
    c = check(repo / ".qq/state", out["header-sha256"])
    assert c.returncode == 0, c.stdout + c.stderr


def test_check_allows_rewrites_of_other_urls(layout):
    """A proxy's ssh-to-https rewrite (url.https://github.com/.insteadOf = git@github.com:) leaves the
    push URL alone."""
    repo, temp, git = layout
    p, out = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    git("config", "--local", "--add", "url.https://github.com/.insteadOf", "git@github.com:")
    git("config", "--local", "--add", "url.https://github.com/.insteadOf", "ssh://git@github.com/")
    c = check(repo / ".qq/state", out["header-sha256"])
    assert c.returncode == 0, c.stdout + c.stderr


def test_check_refuses_a_header_it_did_not_install(layout):
    repo, temp, git = layout
    p, _ = install(repo, temp, JOB_TOKEN="job-token")
    assert p.returncode == 0, p.stderr
    c = check(repo / ".qq/state", hashlib.sha256(header("other").encode()).hexdigest())
    assert c.returncode != 0
    assert "not the one this job installed" in c.stdout
