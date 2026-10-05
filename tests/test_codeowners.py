from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codeowners_names_suraj_for_exactly_the_trust_paths():
    # suraj names the owners (V0-ORG-02); agents must not change them.
    rules = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            path, *owners = line.split()
            rules[path] = owners
    # Exactly these paths: no catch-all and no later line that could leave one unowned (the last
    # match wins).
    owned = {"/.github/", "/pins.toml", "/pyproject.toml", "/src/"}
    # Files setuptools would run at the executor's `pip install .` if anyone added them: owned
    # before they exist, so adding one needs suraj's review.
    guarded = {"/setup.py", "/setup.cfg"}
    assert rules == {path: ["@sharmasuraj0123"] for path in owned | guarded}
    # A renamed path would leave its rule matching nothing.
    assert all((ROOT / path.strip("/")).exists() for path in owned)
