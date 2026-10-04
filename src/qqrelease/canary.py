"""V0-REL-03: the daily canary pipeline v0 (plan §5.8).

Stages, in order; the first failure stops the pipeline and the previous canary stays in place:

1. select      the commit lkgr names. If the canary already names it, or it was held before,
               record a no-op and stop. No lkgr or no manifest yet is a no-op too: nothing to ship.
2. build       every target through its adapter (`qqrecipes execute build`); the artifact digest is
               the digest of the build actions and their outputs.
3. verify      the full test suites (`qqrecipes execute test`).
4. fuzz smoke  property tests only until V1-REL-02 adds fuzzers: the test suites again with wider
               QQ_PROPERTY_* bounds and a seed that changes daily.
5. deploy      start the artifact in the canary test environment (recipes' deploy backend; in v0 the
6. probe       runner) and probe it: the target's own probes, plus every health.toml probe for the
               repo, which must have run and passed. A probe that did not run is a missing signal:
               hold.
7. promote     the release executor moves `channels/canary` to the commit and digest, recording the
               operation key first (V0-REL-02).

The stages after select run on a worker with no write access (`run_stages`); `finish` records the
outcome on the release-state branch and promotes, under the executor's lock. Every run leaves a
record, `canary/<repo>/runs/<date>.json` (schema qq-canary-run/1), which the daily report reads
(V0-REL-04); every held commit leaves `canary/<repo>/held/<commit>.json` so it is not retried.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qqrelease import channels, config
from qqrelease.errors import ReleaseError
from qqrelease.store import Store

CHANNEL = "canary"
RUN_SCHEMA = "qq-canary-run/1"
STAGES_SCHEMA = "qq-canary-stages/1"
MANIFEST = "infra/repo.toml"
# Fuzz smoke bounds. TODO(expert): move to infra-config fuzz.toml when V1-REL-02 adds real fuzzers.
SMOKE_EXAMPLES = "1000"


@dataclass
class Stage:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Selection:
    repo: str
    action: str            # "build" or "noop"
    commit: str = ""
    previous: str = ""     # what the canary names now
    reason: str = ""


@dataclass
class CanaryRun:
    repo: str
    date: str
    outcome: str           # shipped, held (verdicts); noop; error (the pipeline failed: not a record)
    commit: str = ""
    previous: str = ""
    digest: str = ""
    reason: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)
    operation: str = ""
    run_url: str = ""
    started_at: str = ""
    finished_at: str = ""
    schema: str = RUN_SCHEMA

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "CanaryRun":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# --- which repos ship a canary ------------------------------------------------------------------

def canary_repos(cfg: dict) -> list[str]:
    """Repos with a release builder for the canary channel (pipelines.toml) that ship on it
    (repos.toml)."""
    builders = cfg.get("pipelines", {}).get("builder", [])
    names = {b["repo"] for b in builders if b.get("pipeline") == "release" and b.get("channel") == CHANNEL}
    ships = {r["name"] for r in cfg.get("repos", {}).get("repo", []) if CHANNEL in r.get("channels", [])}
    return sorted(names & ships)


# --- stage 1: select ----------------------------------------------------------------------------

def held_path(store: Store, repo: str, commit: str) -> Path:
    store.pointer_path(repo, "x")           # validates the repo name
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ReleaseError(f"not a commit id: {commit!r}")
    return store.root / "canary" / repo / "held" / f"{commit}.json"


def run_path(store: Store, repo: str, date: str) -> Path:
    store.pointer_path(repo, "x")
    if len(date) != 10:
        raise ReleaseError(f"not a date: {date!r}")
    return store.root / "canary" / repo / "runs" / f"{date}.json"


def select(cfg: dict, store: Store, repo: str) -> Selection:
    lkgr = store.pointer(repo, config.lkgr_ref(cfg))
    canary = store.pointer(repo, channels.ref_of(CHANNEL))
    if not lkgr.commit:
        return Selection(repo, "noop", previous=canary.commit, reason="no lkgr yet: no post-submit "
                         "verdict has made a commit known good")
    if lkgr.commit == canary.commit:
        return Selection(repo, "noop", lkgr.commit, canary.commit,
                         f"lkgr has not moved since the last canary ({lkgr.commit[:12]})")
    if held_path(store, repo, lkgr.commit).is_file():
        return Selection(repo, "noop", lkgr.commit, canary.commit,
                         f"lkgr names {lkgr.commit[:12]}, which an earlier canary held; waiting for lkgr to move")
    if any(b.get("commit") == lkgr.commit for b in canary.rolled_back):
        return Selection(repo, "noop", lkgr.commit, canary.commit,
                         f"lkgr names {lkgr.commit[:12]}, which canary was rolled back from; waiting for "
                         "lkgr to move")
    return Selection(repo, "build", lkgr.commit, canary.commit, f"lkgr {lkgr.commit[:12]} is new")


# --- stages 2-6, on a worker --------------------------------------------------------------------

def _recipes(goal: str, src: Path, out: Path, toolchains: list[str], env: dict | None = None,
             timeout: int = 3600) -> tuple[int, list[dict], str]:
    cmd = [sys.executable, "-m", "qqrecipes.cli", "execute", goal, "--repo", str(src), "--out", str(out)]
    for t in toolchains:
        cmd += ["--toolchain", t]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        return 124, [], f"timed out after {timeout} s"
    records = []
    results = out / "results.json"
    if results.is_file():
        try:
            records = json.loads(results.read_text())
        except ValueError:
            records = []
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
    return p.returncode, records, tail


def artifact_digest(records: list[dict]) -> str:
    """The digest of what was built: every build action's own digest and its outputs' digests. The
    same commit built with the same toolchains and adapters gives the same digest."""
    parts = sorted((r.get("target", ""), r.get("name", ""), r.get("digest", ""),
                    sorted((r.get("output_digests") or {}).items()))
                   for r in records if r.get("capability") == "build" and r.get("digest"))
    if not parts:
        return ""
    blob = json.dumps(parts, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(b"qq-canary-artifact/1\n" + blob).hexdigest()


def required_probes(cfg: dict, repo: str) -> list[dict]:
    return [p for p in cfg.get("health", {}).get("probe", []) if p.get("repo") == repo]


def check_probes(records: list[dict], required: list[dict]) -> tuple[bool, str]:
    """Every deployment came up and passed its probes, and every health.toml probe ran and passed."""
    deps = [r["deployment"] for r in records if r.get("deployment")]
    if not deps:
        return False, "nothing was deployed, so nothing could be probed (missing signal: hold)"
    ran: dict[str, bool] = {}
    for d in deps:
        if not d.get("ready"):
            return False, f"{d.get('target')} never became ready: {d.get('ready_detail', '')}"
        for p in d.get("probes", []):
            path = p.get("path", "")
            ran[path] = ran.get(path, True) and bool(p.get("ok"))
    if not ran:
        return False, "no probe ran on any deployment (missing signal: hold)"
    failed = sorted(path for path, ok in ran.items() if not ok)
    if failed:
        return False, "probes failed: " + ", ".join(failed)
    missing = [f"{p['name']} ({p['path']})" for p in required if p.get("path") not in ran]
    if missing:
        return False, ("health.toml probes did not run (add them to the target's probes): "
                       + ", ".join(missing) + " (missing signal: hold)")
    return True, f"{len(ran)} probe path(s) passed on {len(deps)} deployment(s)"


def run_stages(cfg: dict, repo: str, commit: str, src: Path, out: Path, toolchains: list[str],
               date: str) -> dict:
    """Stages 2-6 against a checkout of `commit` at `src`. Returns the stages document."""
    src, out = Path(src), Path(out)
    stages: list[Stage] = []
    digest = ""

    def stage(name: str, fn) -> bool:
        t = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as e:  # a crashed stage is a failed stage, never a pass
            ok, detail = False, f"{type(e).__name__}: {e}"
        stages.append(Stage(name, ok, detail, round(time.monotonic() - t, 1)))
        return ok

    def build():
        nonlocal digest
        rc, recs, tail = _recipes("build", src, out / "build", toolchains)
        if rc != 0:
            return False, "build failed:\n" + tail
        digest = artifact_digest(recs)
        if not digest:
            return False, "the build produced no build action to name an artifact (missing signal: hold)"
        return True, digest

    def verify():
        rc, _, tail = _recipes("test", src, out / "verify", toolchains)
        return (rc == 0, "full test suites passed" if rc == 0 else "tests failed:\n" + tail)

    def fuzz_smoke():
        seed = str(int(hashlib.sha256(f"{repo}/{date}".encode()).hexdigest()[:8], 16))
        rc, _, tail = _recipes("test", src, out / "fuzz-smoke", toolchains,
                               env={"QQ_PROPERTY_EXAMPLES": SMOKE_EXAMPLES, "QQ_PROPERTY_SEED": seed})
        return (rc == 0, f"property tests passed with {SMOKE_EXAMPLES} examples, seed {seed}"
                if rc == 0 else "property tests failed:\n" + tail)

    def deploy_probe():
        rc, recs, tail = _recipes("deploy", src, out / "deploy", toolchains)
        ok, detail = check_probes(recs, required_probes(cfg, repo))
        if rc != 0 and ok:
            ok, detail = False, "deploy failed:\n" + tail
        return ok, detail

    if not (src / MANIFEST).is_file():
        # Not onboarded is a state of the repo, not a bad canary: nothing to build, nothing held.
        return {"schema": STAGES_SCHEMA, "repo": repo, "commit": commit, "digest": "", "date": date,
                "stages": [], "ok": False, "skip": f"{repo} has no {MANIFEST} at {commit[:12]}: not onboarded "
                                                  "yet (V0-ONB-01), so there is nothing to build"}
    for name, fn in (("build", build), ("verify", verify), ("fuzz-smoke", fuzz_smoke),
                     ("deploy-probe", deploy_probe)):
        if not stage(name, fn):
            break
    return {"schema": STAGES_SCHEMA, "repo": repo, "commit": commit, "digest": digest, "date": date,
            "stages": [dataclasses.asdict(s) for s in stages],
            "ok": len(stages) == 4 and all(s.ok for s in stages)}


# --- stage 7 and the record, under the executor's lock ------------------------------------------

STAGES = ("build", "verify", "fuzz-smoke", "deploy-probe")
TERMINAL = ("shipped", "held")


def verdict(stages: dict | None, sel: Selection) -> tuple[str, str]:
    """What the worker's stage results say, recomputed here: the worker runs product code, so its own
    `ok` is never trusted. Returns ("pass" | "fail" | "skip" | "missing", detail)."""
    if stages is None or stages.get("repo") != sel.repo or stages.get("commit") != sel.commit:
        return "missing", "the worker left no stage results for this commit"
    if stages.get("skip"):
        return "skip", str(stages["skip"])
    got = stages.get("stages") or []
    for s in got:
        if s.get("name") in STAGES and s.get("ok") is False:
            return "fail", s["name"]
    if [s.get("name") for s in got] != list(STAGES) or not all(s.get("ok") is True for s in got):
        return "missing", "the stage results are incomplete: " + ", ".join(s.get("name", "?") for s in got)
    if not str(stages.get("digest", "")).startswith("sha256:"):
        return "missing", "the stage results name no sha256 artifact digest"
    return "pass", ""


def finish(cfg: dict, store: Store, mirror, sel: Selection, stages: dict | None, date: str,
           run_url: str = "", started_at: str = "", at: str | None = None) -> CanaryRun:
    """Record the day's canary for one repo and, if every stage passed, promote it.

    Outcomes: `shipped` and `held` are verdicts; `noop` means nothing to do; `error` means the
    pipeline itself failed (lost worker, missing results, a promote that could not be written). An
    error holds nothing and is not a day's record, so the watchdog runs that day again."""
    from qqrelease import executor

    at = at or executor.now_iso()
    run = CanaryRun(repo=sel.repo, date=date, outcome="noop", commit=sel.commit, previous=sel.previous,
                    reason=sel.reason, run_url=run_url, started_at=started_at or at, finished_at=at)
    files: dict[Path, str] = {}
    if sel.action == "build":
        v, detail = verdict(stages, sel)
        if v != "missing":
            run.stages, run.digest = stages.get("stages", []), stages.get("digest", "")
        if v == "skip":
            run.reason = detail
        elif v == "missing":
            run.outcome, run.reason = "error", f"not judged: {detail}; the watchdog runs this day again"
        elif v == "fail":
            failed = next(s for s in run.stages if s.get("name") == detail)
            first = (failed.get("detail") or "").splitlines()
            run.outcome = "held"
            run.reason = f"held at {detail}: {first[0] if first else ''}"
            files[held_path(store, sel.repo, sel.commit)] = json.dumps(
                {"repo": sel.repo, "commit": sel.commit, "date": date, "stage": detail,
                 "digest": run.digest, "run_url": run_url}, sort_keys=True, indent=2) + "\n"
        else:
            ref = channels.ref_of(CHANNEL)
            try:
                executor.finish_pending(store, mirror, sel.repo, ref, at=at,
                                        derived=channels.derived(store))
                cur = store.pointer(sel.repo, ref)
                if (cur.commit, cur.digest) == (sel.commit, run.digest):
                    detail = f"channels/canary already names {sel.commit[:12]} (a rerun)"
                    run.operation = cur.op
                else:
                    op = channels.plan_promote(cfg, store, sel.repo, CHANNEL, sel.commit, run.digest,
                                               reason=f"daily canary {date}", actor=mirror.actor())
                    op, _ = channels.apply(store, mirror, op, at=at)
                    detail, run.operation = f"channels/canary -> {sel.commit[:12]} ({op.mirror})", op.key
                run.outcome = "shipped"
                run.stages = run.stages + [{"name": "promote", "ok": True, "seconds": 0, "detail": detail}]
                run.reason = f"shipped {sel.commit[:12]} {run.digest}"
            except ReleaseError as e:
                run.outcome = "error"
                run.reason = f"passed every stage but the promote failed: {e}; the watchdog runs this day again"
    path = run_path(store, sel.repo, date)
    doc = dataclasses.asdict(run)
    if path.is_file():
        # Another run on the same day (the watchdog, a retry, a manual dispatch). The day's verdict
        # stays on top and what came after is kept under `later`; a verdict replaces an earlier no-op
        # or error, and a hold always goes on top, so the report never hides it.
        prior = json.loads(path.read_text())
        if prior.get("outcome") in TERMINAL and run.outcome != "held":
            doc = {**prior, "later": prior.get("later", []) + [doc]}
        else:
            earlier = prior.pop("earlier", []) + [{k: v for k, v in prior.items() if k != "later"}]
            doc = {**doc, "earlier": earlier}
    files[path] = json.dumps(doc, sort_keys=True, indent=2) + "\n"
    store.save(files, f"canary {sel.repo} {date}: {run.outcome} {sel.commit[:12]}")
    return run


def runs_on(store: Store, repo: str, date: str) -> list[dict]:
    """Every run recorded for `repo` on `date`: the top-level one, then `earlier`, then `later`."""
    path = run_path(store, repo, date)
    if not path.is_file():
        return []
    top = json.loads(path.read_text())
    flat = {k: v for k, v in top.items() if k not in ("earlier", "later")}
    return [flat] + top.get("earlier", []) + top.get("later", [])


def missing_runs(cfg: dict, store: Store, date: str) -> list[str]:
    """Canary repos with no run record for `date`: what the watchdog starts."""
    # TODO(expert): a day that keeps ending in error (say a worker that always reports the wrong
    # commit) is rerun twice a day without escalating; v1 should file a failure record after N.
    def judged(r: str) -> bool:
        p = run_path(store, r, date)
        return p.is_file() and json.loads(p.read_text()).get("outcome") != "error"
    return [r for r in canary_repos(cfg) if not judged(r)]


def toolchain_versions(src: Path) -> dict[str, str]:
    """name -> version of each toolchain the repo's manifest pins, read through sync (`qqsync show`),
    never by parsing the manifest here."""
    p = subprocess.run([sys.executable, "-m", "qqsync.cli", "show", str(Path(src) / MANIFEST)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ReleaseError(f"qqsync show {Path(src) / MANIFEST}: {p.stderr.strip()}")
    doc = json.loads(p.stdout)
    return {name: str(t.get("version", "")) for name, t in sorted(doc.get("toolchains", {}).items())}


POSTMORTEM_TEMPLATE = "templates/postmortem.md"


def postmortem_draft(cfg_root: Path, run: CanaryRun, failure_issue: str = "") -> str:
    """A postmortem draft for a held canary, filled from captured evidence (postmortem.toml trigger
    `canary-deploy-failed`). Every section of infra-config's template is kept, in its order; v0 adds
    the stage table under Summary. TODO(expert): an agent completes the narrative sections
    (V1-GAR-03); v0 fills only what the pipeline captured."""
    draft = _postmortem_body(run, failure_issue)
    tmpl = Path(cfg_root) / POSTMORTEM_TEMPLATE
    if tmpl.is_file():
        have = {l for l in draft.splitlines() if l.startswith("## ")}
        for heading in (l for l in tmpl.read_text().splitlines() if l.startswith("## ")):
            if heading not in have:
                draft += f"\n{heading}\n\nTODO(agent): fill in from the template.\n"
    return draft


def _postmortem_body(run: CanaryRun, failure_issue: str) -> str:
    failed = next((s for s in run.stages if not s["ok"]), {"name": "?", "detail": ""})
    rows = "\n".join(f"| {s['name']} | {'pass' if s['ok'] else '**fail**'} | {s['seconds']} s |"
                     for s in run.stages)
    detail = failed["detail"].replace("```", "'''")
    return f"""# Postmortem: canary held for {run.repo} at {failed['name']} ({run.date})

**Trigger:** canary-deploy-failed
**Repo or area:** {run.repo}
**Status:** draft
**Failure record:** {failure_issue or '(see the qq-failure issue for this canary)'}

## Summary

The daily canary for {run.repo} on {run.date} built `{run.commit[:12]}` (artifact `{run.digest or 'none'}`)
from lkgr and stopped at the **{failed['name']}** stage. Nothing was deployed to the canary channel:
`channels/canary` still names `{run.previous[:12] or '(nothing yet)'}`.

## Timeline (UTC)

| Time | Event | Evidence |
|---|---|---|
| {run.started_at} | canary started | {run.run_url} |
| {run.finished_at} | held at {failed['name']} | {run.run_url} |

## Stages

| Stage | Result | Time |
|---|---|---|
{rows}

```
{detail}
```

## Impact

None outside the canary test environment: a held canary is never deployed (plan §5.8). lkgr's commit
`{run.commit[:12]}` will not be retried; the next canary waits for lkgr to move.

## Root cause and trigger

TODO(agent): find the culprit between `{run.previous[:12] or 'the first canary'}` and `{run.commit[:12]}`.

## Record needs

- [ ] Culprit:
- [ ] Fix:
- [ ] Covering test:

## Action items

| Action | Owner | Issue |
|---|---|---|
| | | |

## Lessons learned

TODO(agent): what went well, what went badly, and where we got lucky.
"""
