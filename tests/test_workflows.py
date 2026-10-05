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
    install = index(lambda s: "pip install" in s.get("run", ""))
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
    python = [i for i, s in enumerate(ss) if re.search(r"\bpython3?\b", s.get("run", ""))]
    setup = index(lambda s: str(s.get("uses", "")).startswith("actions/setup-python@"))
    assert python and first < setup < min(python)
    for i in python:
        assert ss[i].get("env", {}).get("PYTHONSAFEPATH") == "1", ss[i]


def test_no_repository_hook_runs_once_the_credential_is_installed():
    ss = steps()
    cred = index(lambda s: s.get("id") == "push-credential")
    for s in ss[cred + 1:]:
        for line in s.get("run", "").splitlines():
            for call in re.findall(r"\bgit\b[^|;&]*", line.split("#")[0]):
                assert call.startswith("git -c core.hooksPath=/dev/null "), line


WORKFLOWS = ROOT / ".github" / "workflows"
# The jobs that push release-state: they alone may hold the App's key (the release-executor
# environment, which only main can use).
WRITERS = {("lkgr.yml", "lkgr"), ("canary.yml", "finish"), ("canary.yml", "report"),
           ("channel-rollback.yml", "rollback"), ("canary-release-hold.yml", "release-hold")}
KEY = "${{ secrets.QQ_RELEASE_PRIVATE_KEY }}"


def jobs():
    for f in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(f.read_text())["jobs"].items():
            yield (f.name, name), job


def test_only_the_writer_jobs_run_in_the_release_executor_environment():
    found = {k for k, job in jobs() if "environment" in job}
    assert found == WRITERS
    # And the writers are exactly the jobs that run the executor with writes on.
    writes = {k for k, job in jobs() for s in job.get("steps", [])
              if s.get("uses") == "./.github/actions/executor" and s.get("with", {}).get("writes") == "true"}
    assert writes == WRITERS
    assert all(job["environment"] == "release-executor" for k, job in jobs() if k in WRITERS)


def test_writer_jobs_install_their_own_credential_and_upload_nothing():
    for k, job in jobs():
        if k not in WRITERS:
            continue
        ss = job["steps"]
        [checkout] = [s for s in ss if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert checkout.get("with", {}).get("persist-credentials") is False, k
        [ex] = [s for s in ss if s.get("uses") == "./.github/actions/executor"]
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
    for f in WORKFLOWS.glob("*.yml"):
        top = {k: v for k, v in yaml.safe_load(f.read_text()).items() if k != "jobs"}
        assert "QQ_RELEASE_PRIVATE_KEY" not in yaml.safe_dump(top), f.name
