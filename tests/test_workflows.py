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
