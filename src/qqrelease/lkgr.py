"""V0-REL-01: advance `lkgr` to the newest main commit whose required post-submit builders are all
green.

The verdicts are the gardener's: the same first-parent history, the same run that counts per
(builder, commit) (the newest run, its newest attempt), the same states (qqgarden, pinned). A commit
qualifies only when every builder has a completed, passing run on it. Pending, missing and cancelled
are not green: a missing signal holds lkgr where it is.

`lkgr` only moves forward along main, with one exception: if the commit it names turns red (a
re-run failed), it moves back to the newest all-green commit, so it never names a red commit.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from qqgarden.model import BuilderRun, Commit, RunState
from qqgarden.postsubmit import latest_runs, state_of


@dataclass(frozen=True)
class Decision:
    action: str            # "advance", "retreat", "stay", "hold", "stuck"
    commit: str            # where lkgr should point ("" for hold/stuck with no candidate)
    reason: str


def all_green(commit: Commit, builders: Sequence[str], latest: dict, now: datetime,
              grace: timedelta) -> bool:
    return all(state_of(commit, b, latest, now, grace) is RunState.GREEN for b in builders)


def any_red(commit: Commit, builders: Sequence[str], latest: dict, now: datetime,
            grace: timedelta) -> list[str]:
    return [b for b in builders if state_of(commit, b, latest, now, grace) is RunState.RED]


def decide(builders: Sequence[str], commits: Sequence[Commit], runs: Iterable[BuilderRun],
           current: str, now: datetime, grace: timedelta) -> Decision:
    """`commits` is main's first-parent history, newest first; `current` is where lkgr points."""
    if not builders:
        return Decision("hold", current, "infra-config defines no post-submit builder for this repo, "
                                         "so no commit can be known good")
    latest = latest_runs(runs)
    index = {c.sha: i for i, c in enumerate(commits)}
    candidate = next((c for c in commits if all_green(c, builders, latest, now, grace)), None)

    cur_red: list[str] = []
    if current in index:
        cur_red = any_red(commits[index[current]], builders, latest, now, grace)

    if candidate is None:
        if cur_red:
            return Decision("stuck", current, f"lkgr {current[:12]} is now red on {', '.join(cur_red)} and "
                                              "no listed main commit is green on every builder")
        return Decision("hold", current, "no listed main commit has a green run on every post-submit "
                                         "builder (" + ", ".join(builders) + ")")
    if candidate.sha == current:
        return Decision("stay", current, f"{current[:12]} is still the newest all-green commit")
    if cur_red:
        verb = "advance" if index[candidate.sha] < index[current] else "retreat"
        return Decision(verb, candidate.sha, f"lkgr {current[:12]} turned red on {', '.join(cur_red)}; "
                                             f"{candidate.sha[:12]} is the newest all-green commit")
    if not current:
        return Decision("advance", candidate.sha, f"first lkgr: {candidate.sha[:12]} is the newest "
                                                  "all-green commit")
    if current not in index:
        # Older than the listed window (or no longer on main). TODO(expert): check ancestry with the
        # backend instead of assuming that a commit outside the window is older.
        return Decision("advance", candidate.sha, f"{candidate.sha[:12]} is the newest all-green commit; "
                                                  f"lkgr {current[:12]} is older than the listed window")
    if index[candidate.sha] < index[current]:
        return Decision("advance", candidate.sha, f"{candidate.sha[:12]} is newer than lkgr and green on "
                                                  "every post-submit builder")
    return Decision("stay", current, f"lkgr {current[:12]} is newer than every other all-green commit")
