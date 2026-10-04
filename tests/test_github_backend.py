import pytest

from qqrelease.backends.github import Mirror
from qqrelease.errors import ReleaseError


class Fake(Mirror):
    def __init__(self, refs):
        super().__init__(repos={"demo": "quirq-ai/demo"}, token="t")
        self.refs, self.calls = refs, []

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            sha = self.refs.get(path.rsplit("/heads/", 1)[1])
            return {"object": {"sha": sha}} if sha else None
        return {}


def test_creates_a_missing_ref():
    m = Fake({})
    assert m.write_ref("demo", "channels/canary", [""], "b" * 40) == "pushed"
    assert m.calls[-1] == ("POST", "/repos/quirq-ai/demo/git/refs",
                           {"ref": "refs/heads/channels/canary", "sha": "b" * 40})


def test_moves_an_existing_ref_only_from_the_expected_commit():
    m = Fake({"lkgr": "a" * 40})
    assert m.write_ref("demo", "lkgr", ["a" * 40], "b" * 40) == "pushed"
    assert m.calls[-1][0] == "PATCH"
    with pytest.raises(ReleaseError, match="something else moved it"):
        Fake({"lkgr": "c" * 40}).write_ref("demo", "lkgr", ["a" * 40], "b" * 40)


def test_a_ref_already_at_the_target_is_done():
    assert Fake({"lkgr": "b" * 40}).write_ref("demo", "lkgr", ["a" * 40], "b" * 40) == "already there"


def test_without_a_token_nothing_is_written():
    m = Mirror(repos={"demo": "quirq-ai/demo"}, token="")
    assert m.write_ref("demo", "lkgr", [""], "b" * 40).startswith("skipped")
