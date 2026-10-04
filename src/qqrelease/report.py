"""V0-REL-04: the daily canary report (plan §5.8 "Everything else goes into one daily canary report").

One report per day, built from the day's run records on release-state (`canary/<repo>/runs/<date>.json`)
and the open failure records (test-pipelines' `qq-failure` issues, V0-TST-04): what shipped, what
was held and why, what was a no-op, what each channel names now, and which records are still open.
The `canary` workflow files it as one issue per day, labelled `canary-report`.
"""
from __future__ import annotations

import json
from pathlib import Path

from qqrelease import canary, channels
from qqrelease.store import Store

LABEL = "canary-report"
OPEN_LIMIT = 200          # the workflow's `gh issue list --limit`


def title(date: str) -> str:
    return f"Canary report {date}"


def _cell(text: str) -> str:
    """Text from run records (a stage's output line comes from product code) as one inert table
    cell: no pipes, no HTML or comments, no @mentions or #references."""
    t = " ".join(str(text).split())
    for a, b in (("|", "/"), ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("@", "@\u200b"),
                 ("#", "#\u200b"), ("`", "'")):
        t = t.replace(a, b)
    return t


def _code(text: str) -> str:
    return f"`{text}`" if text else "(none)"


def build(cfg: dict, store: Store, date: str, open_records: list[dict] | None = None) -> str:
    """Markdown for `date`. `open_records` are open failure issues: [{number, title, url}], or None
    when they could not be read (said so, never shown as none)."""
    repos = canary.canary_repos(cfg)
    rows, shipped, held, noop, errors, missing = [], 0, 0, 0, 0, []
    for repo in repos:
        path = canary.run_path(store, repo, date)
        if not path.is_file():
            missing.append(repo)
            rows.append(f"| {repo} | **no run** | | | the canary did not run for this repo today |")
            continue
        try:
            run = json.loads(path.read_text())
            outcome = str(run["outcome"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            rows.append(f"| {repo} | **unreadable** | | | {_cell(type(e).__name__)}: the run record could not be read |")
            continue
        shipped += outcome == "shipped"
        held += outcome == "held"
        noop += outcome == "noop"
        errors += outcome == "error"     # the pipeline failed, not the commit; the watchdog reruns it
        link = f"[run]({run['run_url']})" if run.get("run_url") else ""
        later = [r.get("outcome", "?") for r in run.get("later", []) if r.get("outcome") != "noop"]
        also = f" (later the same day: {', '.join(_cell(o) for o in later)})" if later else ""
        rows.append(f"| {repo} | **{_cell(outcome)}** | {_code(_cell(run.get('commit', ''))[:12])} "
                    f"| {_code(_cell(run.get('digest', ''))[:19])} | {_cell(run.get('reason', ''))}{also} {link} |")
    names = []
    for repo in repos:
        p = store.pointer(repo, channels.ref_of(canary.CHANNEL))
        names.append(f"| {repo} | {_code(p.commit[:12]) if p.commit else '(nothing yet)'} | {_code(p.digest[:19])} "
                     f"| {p.updated_at or '(never)'} |")
    if open_records is None:
        records = "Could not read the open failure records today; check the `qq-failure` issues."
    elif not open_records:
        records = "None."
    else:
        records = "\n".join(f"- [#{r['number']}]({r['url']}) {_cell(r['title'])}" for r in open_records)
        if len(open_records) >= OPEN_LIMIT:
            records += f"\n- ...and possibly more: the list stops at {OPEN_LIMIT}; see the `qq-failure` issues."
    head = (f"{shipped} shipped, {held} held, {noop} no-op" + (f", {errors} pipeline error" if errors else "")
            + (f", {len(missing)} did not run" if missing else ""))
    return f"""# {title(date)}

{head}.

## Today

| Repo | Outcome | Commit | Artifact | Why |
|---|---|---|---|---|
{chr(10).join(rows)}

## What canary names now

| Repo | Commit | Artifact | Since |
|---|---|---|---|
{chr(10).join(names)}

## Open failure records

{records}

_Written by the `canary` workflow from `release-state` (V0-REL-04). Held canaries also get a
postmortem draft labelled `postmortem`._
"""


def write(cfg: dict, store: Store, date: str, open_records: list[dict] | None) -> Path:
    """Keep the report on release-state too (`reports/<date>.md`), so it outlives the issue tracker."""
    path = store.root / "reports" / f"{date}.md"
    store.save({path: build(cfg, store, date, open_records)}, f"canary report {date}")
    return path
