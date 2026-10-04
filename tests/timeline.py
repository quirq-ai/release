"""Builds main histories and post-submit runs for tests: one letter per commit and builder.

    g green, r red, p running, m no run, c cancelled, R red then re-run green, G green then re-run red
"""
from datetime import datetime, timedelta, timezone

from qqgarden.model import BuilderRun, Commit

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=15)


def sha(i: int) -> str:
    return f"{i:040x}"


def history(**builders: str) -> tuple[list[Commit], list[BuilderRun]]:
    """history(a="ggr", b="ggg"): commits oldest first, one letter per builder. Returns commits newest
    first, as a backend does, and every builder's runs."""
    n = len(next(iter(builders.values())))
    commits = [Commit(sha=sha(i + 1), title=f"commit {i + 1}",
                      landed_at=(NOW - timedelta(minutes=60 + (n - 1 - i) * 20)).isoformat())
               for i in range(n)]
    runs = []
    for b, states in builders.items():
        assert len(states) == n
        runs += runs_for(b, commits, states)
    return list(reversed(commits)), runs


def runs_for(builder: str, commits: list[Commit], states: str, first_id: int = 1000) -> list[BuilderRun]:
    """`commits` oldest first."""
    out = []
    for i, (c, ch) in enumerate(zip(commits, states)):
        rid = str(first_id + i)
        url = f"https://example.invalid/runs/{rid}"
        if ch == "g":
            out.append(BuilderRun(builder, c.sha, "completed", "success", rid, 1, url))
        elif ch == "r":
            out.append(BuilderRun(builder, c.sha, "completed", "failure", rid, 1, url))
        elif ch == "p":
            out.append(BuilderRun(builder, c.sha, "in_progress", "", rid, 1, url))
        elif ch == "c":
            out.append(BuilderRun(builder, c.sha, "completed", "cancelled", rid, 1, url))
        elif ch == "R":
            out += [BuilderRun(builder, c.sha, "completed", "failure", rid, 1, url),
                    BuilderRun(builder, c.sha, "completed", "success", rid, 2, url)]
        elif ch == "G":
            out += [BuilderRun(builder, c.sha, "completed", "success", rid, 1, url),
                    BuilderRun(builder, c.sha, "completed", "failure", rid, 2, url)]
        elif ch != "m":
            raise ValueError(ch)
    return out
