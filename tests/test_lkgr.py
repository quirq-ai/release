import pytest

from qqrelease.lkgr import decide
from timeline import GRACE, NOW, history, sha

B = ["a-postsubmit"]


def run(current="", **builders):
    commits, runs = history(**builders)
    return decide(list(builders), commits, runs, current, NOW, GRACE)


def test_first_lkgr_is_the_newest_all_green_commit():
    d = run(a="ggrg")
    assert (d.action, d.commit) == ("advance", sha(4))


def test_red_pending_missing_and_cancelled_are_never_lkgr():
    for tail in "rpmc":
        d = run(a="gg" + tail)
        assert (d.action, d.commit) == ("advance", sha(2)), tail


def test_every_builder_must_be_green_on_the_same_commit():
    d = run(a="gggg", b="ggrp")
    assert (d.action, d.commit) == ("advance", sha(2))


def test_moves_forward_only():
    assert run(current=sha(2), a="ggrg").action == "advance"
    d = run(current=sha(3), a="gggr")
    assert (d.action, d.commit) == ("stay", sha(3))


def test_stays_when_nothing_newer_is_green():
    d = run(current=sha(3), a="gggpp")
    assert (d.action, d.commit) == ("stay", sha(3))


def test_a_rerun_that_turns_lkgr_red_moves_it_back():
    d = run(current=sha(3), a="ggG")          # lkgr's commit re-ran red
    assert (d.action, d.commit) == ("retreat", sha(2))


def test_a_red_lkgr_with_nothing_green_is_stuck():
    d = run(current=sha(1), a="Grr")
    assert d.action == "stuck"


def test_no_signal_holds():
    assert run(a="pmc").action == "hold"
    commits, runs = history(a="ggg")
    assert decide([], commits, runs, "", NOW, GRACE).action == "hold"


def test_a_rerun_to_green_counts():
    d = run(a="gR")
    assert (d.action, d.commit) == ("advance", sha(2))


@pytest.mark.parametrize("states", ["gggrgrg", "grgrgrg", "rrggrrg", "gRGgRrg"])
def test_never_names_a_red_commit(states):
    """Replays a growing main one commit at a time: lkgr is never red, and never moves back unless
    its own commit turned red."""
    current = ""
    for n in range(1, len(states) + 1):
        commits, runs = history(a=states[:n])
        d = decide(["a"], commits, runs, current, NOW, GRACE)
        if d.commit:
            i = int(d.commit, 16) - 1
            assert states[i] in "gR", (states[:n], d)
        current = d.commit
