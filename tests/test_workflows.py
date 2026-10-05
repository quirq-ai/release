"""Lint for the executor action: how a writer job gets its release-state push credential, and what
runs before and while it holds it."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "executor" / "action.yml"
MINT = "actions/create-github-app-token@"


def steps():
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def index(pred) -> int:
    [i] = [i for i, s in enumerate(steps()) if pred(s)]
    return i


def test_no_step_continues_on_error():
    # A failed mint must fail the job, never fall back to GITHUB_TOKEN.
    assert not [s for s in steps() if "continue-on-error" in s]


def test_the_push_token_is_minted_for_this_repo_only_after_the_install():
    ss = steps()
    mint = index(lambda s: str(s.get("uses", "")).startswith(MINT))
    install = index(lambda s: "install.sh" in s.get("run", ""))
    guard = index(lambda s: s.get("name") == "writers run from main only")
    cred = index(lambda s: s.get("id") == "push-credential")
    assert guard < install < mint < cred
    w = ss[mint]["with"]
    assert w["repositories"] == "release"
    assert w["permission-contents"] == "write"
    assert w["owner"] == "${{ github.repository_owner }}"
    assert not [k for k in w if k.startswith("permission-") and k != "permission-contents"]
    assert ss[mint]["if"] == "inputs.writes == 'true' && inputs.client-id != ''"
    # The only mint in the action; the key reaches it as an input, never from secrets directly.
    assert sum(str(s.get("uses", "")).startswith(MINT) for s in ss) == 1
    assert "secrets." not in ACTION.read_text()


def test_python_never_imports_from_the_working_directory():
    """An unowned root `tomllib.py` or `pip/` would run at `python -c` or `python -m pip` and could plant
    code that runs later with the push credential."""
    ss = steps()
    first = index(lambda s: "PYTHONSAFEPATH=1" in s.get("run", "") and "GITHUB_ENV" in s["run"])
    python = [i for i, s in enumerate(ss) if re.search(r"\bpython3?\b|install\.sh", s.get("run", ""))]
    setup = index(lambda s: str(s.get("uses", "")).startswith("actions/setup-python@"))
    assert python and first < setup < min(python)
    # Job-wide for writers only: canary stages run product commands that may import siblings.
    assert ss[first]["if"] == "inputs.writes == 'true'"
    for i in python:
        assert ss[i].get("env", {}).get("PYTHONSAFEPATH") == "1", ss[i]


def test_no_repository_hook_runs_once_the_credential_is_installed():
    ss = steps()
    cred = index(lambda s: s.get("id") == "push-credential")
    for s in ss[cred + 1:]:
        for line in s.get("run", "").splitlines():
            for call in re.findall(r"\bgit\b[^|;&]*", line.split("#")[0]):
                assert call.startswith("git -c core.hooksPath=/dev/null "), line


def test_the_install_takes_pypi_packages_by_hash_and_qq_packages_by_commit():
    """F2: nothing unpinned from PyPI runs before the token is minted."""
    script = (ACTION.parent / "install.sh").read_text()
    assert "--require-hashes --only-binary=:all: --no-deps -r \"$here/requirements.txt\"" in script
    assert "--no-deps --no-build-isolation -r \"$here/requirements-qq.txt\" ." in script
    lines = [l for l in (ACTION.parent / "requirements.txt").read_text().splitlines()
             if l.strip() and not l.lstrip().startswith(("#", "--hash"))]
    assert all(re.match(r"^[A-Za-z0-9._-]+==[^ ;]+ \\$", l) for l in lines), lines
    text = (ACTION.parent / "requirements.txt").read_text()
    assert text.count("--hash=sha256:") == len(lines)


INSTALLER = re.compile(r"\b(pip3?|uv|easy_install|ensurepip)\b")


def test_the_action_installs_only_through_install_sh_and_writers_install_nothing():
    runs = [s.get("run", "") for s in steps()]
    assert sum(r.strip() == 'bash "$GITHUB_ACTION_PATH/install.sh"' for r in runs) == 1
    assert not [r for r in runs if INSTALLER.search(r)]
    for k, job in jobs():
        if k in WRITERS:
            assert not [s for s in job["steps"] if INSTALLER.search(s.get("run", ""))], k
    code = "\n".join(l.split("#")[0] for l in (ACTION.parent / "install.sh").read_text().splitlines())
    assert len(re.findall(r"\bpip3?\s+install\b", code)) == 2
    assert re.search(r"^set -euo pipefail$", code, re.M) and re.search(r"^python -m pip check$", code, re.M)


def test_no_build_output_is_committed_or_installed():
    """B1: setuptools ships whatever build/lib holds, so a committed file there would run with the
    credential, and a stale copy could win over src/."""
    import subprocess
    assert subprocess.run(["git", "ls-files", "build"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout == ""
    code = [l.split("#")[0].strip() for l in (ACTION.parent / "install.sh").read_text().splitlines()]
    second = next(i for i, l in enumerate(code) if "requirements-qq.txt" in l)
    assert "rm -rf build" in code[:second]


def test_the_qq_commits_installed_by_the_executor_are_the_ones_pyproject_resolves():
    """Checked against what pip installed from pyproject.toml here (qqsync through qqrecipes)."""
    import json
    from importlib import metadata
    pins = {}
    for line in (ACTION.parent / "requirements-qq.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, url = [x.strip() for x in line.split(" @ ")]
            pins[name] = url.rsplit("@", 1)[1]
    assert set(pins) == {"qqsync", "qqgarden", "qqrecipes"}
    for name, commit in pins.items():
        info = json.loads(metadata.distribution(name).read_text("direct_url.json"))
        assert info["vcs_info"]["commit_id"] == commit, name


WORKFLOWS = ROOT / ".github" / "workflows"
# The jobs that push release-state: they alone may hold the App's key (the release-executor
# environment, which only main can use).
WRITERS = {("lkgr.yml", "lkgr"), ("canary.yml", "finish"), ("canary.yml", "report"),
           ("channel-rollback.yml", "rollback"), ("canary-release-hold.yml", "release-hold")}
KEY = "${{ secrets.QQ_RELEASE_PRIVATE_KEY }}"


def jobs():
    for f in sorted(WORKFLOWS.glob("*.y*ml")):
        for name, job in yaml.safe_load(f.read_text())["jobs"].items():
            yield (f.name, name), job


def is_executor(step) -> bool:
    return str(step.get("uses", "")).rstrip("/") == "./.github/actions/executor"


def writes(step) -> bool:
    return str(step.get("with", {}).get("writes", "")).lower() == "true"


def test_only_the_writer_jobs_run_in_the_release_executor_environment():
    found = {k for k, job in jobs() if "environment" in job}
    assert found == WRITERS
    # And the writers are exactly the jobs that run the executor with writes on.
    writers = {k for k, job in jobs() for s in job.get("steps", [])
              if is_executor(s) and writes(s)}
    assert writers == WRITERS
    assert all(job["environment"] == "release-executor" for k, job in jobs() if k in WRITERS)


def test_writer_jobs_install_their_own_credential_and_upload_nothing():
    for k, job in jobs():
        if k not in WRITERS:
            continue
        ss = job["steps"]
        [checkout] = [s for s in ss if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert checkout.get("with", {}).get("persist-credentials") is False, k
        [ex] = [s for s in ss if is_executor(s)]
        assert ex["with"] == {"writes": "true", "client-id": "${{ vars.QQ_RELEASE_CLIENT_ID }}",
                              "private-key": KEY}, k
        assert ss.index(checkout) < ss.index(ex)
        assert not [s for s in ss if str(s.get("uses", "")).startswith("actions/upload-artifact@")], k


def test_the_key_appears_in_no_other_job():
    """Not plan, not stages (which runs product code), not held, the watchdog or presubmit."""
    for k, job in jobs():
        if k not in WRITERS:
            assert "QQ_RELEASE_PRIVATE_KEY" not in yaml.safe_dump(job), k
    # And nowhere at workflow level, where every job would see it.
    for f in WORKFLOWS.glob("*.y*ml"):
        top = {k: v for k, v in yaml.safe_load(f.read_text()).items() if k != "jobs"}
        assert "QQ_RELEASE_PRIVATE_KEY" not in yaml.safe_dump(top), f.name
