# release

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo moves
builds through channels. A channel is a pointer: `channels/<name>` names a commit and an artifact
digest, and only the release executor moves it, recording an operation key before it does.

- `lkgr` (last known good): the newest `main` commit whose required post-submit builders are all
  green. Channels are cut from it.
- `canary`: built and shipped daily by agents alone, to quirq's research and test environments only.
- `dev` (declared, off in v0) and `stable` (suraj promotes; low priority, v2).

**Chromium counterpart:** V8's lkgr finder and release scripts, and LUCI's `promote.py`.

Plan and every v0 item: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## Rules it lives by

- Rules come from infra-config's `channels.toml` and `health.toml`, read through infra-config's `qqcfg` (via the gardener's pinned reader) at the
  commit pinned in `pins.toml`. Changing them is a policy change for suraj.
- Record an operation key before every external effect, so a retry never acts twice.
- A missing signal means hold, never promote.
- Schedules can be dropped: run off the hour, with a watchdog for missed runs.
- GitHub-specific code sits behind the `backend` field (`github` now, `launchpad` later).

## lkgr (V0-REL-01)

`lkgr` is the newest main commit whose required post-submit builders are all green. "Required" and
"green" are the gardener's definitions, read through `qqgarden` at a pinned commit: the repo's
`postsubmit` builders triggered by `land` in infra-config `pipelines.toml`, and per builder and commit
the newest run's newest attempt. Pending, missing and cancelled are not green, so a missing signal
holds `lkgr` where it is. It moves forward along main only, except that if a re-run turns its own
commit red it moves back to the newest all-green commit: it never names a red commit.

```sh
qqrelease lkgr --config <infra-config checkout> --state <state dir> [--publish] [--dry-run]
qqrelease lkgr --config ... --state ... --backend local --target-root DIR --snapshot SNAP.json   # offline
```

The `lkgr` workflow runs it every 10 minutes, off the hour. Each move is an operation of the release
executor (below). `tools/lkgr_demo.py` replays a growing main with late, red and re-run verdicts
against local repos and checks `lkgr` moves on its own and is never red; presubmit runs it.

## The release executor and its record

Only the executor moves `lkgr` and `channels/*`. Every move is an **operation**: its key hashes the
intent (repo, ref, from, to, digest) and the pointer's generation, and is committed and pushed to
this repo's `release-state` branch **before** anything else changes. Then the target repo's git ref
moves (compare-and-swap: it refuses if something else moved it), then the new pointer and the
applied operation are recorded together. A retry with the same intent is a no-op. Until its
result is recorded the pointer names the operation as `pending`, and every run settles a pending
operation before planning a new one. Settling reads the target ref and never writes it: if the
write landed (a run died after it, or timed out on it) the operation is applied; if it did not, the
operation is abandoned and the move is planned again from fresh verdicts, so a stale move to a
commit that has since turned red is never made. The one case lkgr cannot fix alone is `stuck`: its
commit turned red and no listed commit is green; it stays put and the workflow fails loudly.

    release-state branch
      pointers/<repo>/<ref>.json   what lkgr and each channel name (schema qq-pointer/1), with history
      ops/<key>.json               every operation: recorded, applied or failed (schema qq-operation/1)

On GitHub the ref is the branch `<ref>` in the target repo (`lkgr`, `channels/canary`), written
with the executor's App token (`QQ_RELEASE_TOKEN`). gate's `qq-release-refs` rulesets let only
that identity write them. **TODO(suraj):** the identity does not exist yet. Until it does, the ref
write is skipped and the operation records `skipped: no release executor identity`; the pointer
still moves in `release-state`, which is the record readers use. The pointer remembers that the
ref was not written (`mirrored: false`), so the first write after the identity exists creates the
ref instead of refusing it as moved by someone else.

## Channels and rollback (V0-REL-02)

Channels and their order come from infra-config `channels.toml`; which channels a repo ships on,
from `repos.toml`. A channel's pointer `channels/<name>` names a commit **and** the digest
(`sha256:...`) of the artifact built from it.

```sh
qqrelease channel promote  --config ... --state ... --repo NAME --channel canary --commit SHA --digest sha256:...
qqrelease channel rollback --config ... --state ... --repo NAME --channel canary --reason "..." --from COMMIT
qqrelease channel show     --state ...
qq channel rollback ...    # the same command in depot's qq (entry point qq.commands)
```

- `promote` only accepts the commit the channel's source names now: canary takes `lkgr`'s, dev
  takes canary's. The daily canary pipeline (V0-REL-03) is what calls it.
- `rollback` points the channel at the newest earlier commit and digest from the pointer's history
  that it was never rolled back from. Nothing is rebuilt. The value it moves away from is recorded
  as rolled back: a second rollback goes further back, never forward, and a promotion never ships it
  again. Retrying a rollback whose write already landed does nothing more, and with `--from` (the commit the channel names now) a re-run or double
  dispatch is refused instead of rolling back twice. The `channel-rollback`
  workflow runs it on demand.
- A promotion that changes nothing is refused. When a channel takes its build from another channel
  (dev from canary), it takes that channel's digest too, so only a vetted artifact moves on.
- v0 promotes only channels whose `channels.toml` rules need no person and no signal it cannot read
  yet (canary). A channel that needs an approval, a soak or health signals (dev, stable) is refused,
  and so is a rollback whose `[channel.rollback]` needs an approval: held, never done unchecked.
- After every move, `channels.json` on `release-state` (schema `qq-channels/1`) says what each
  repo's channels name: commit, digest, generation, operation and time. The installer reads it
  (V0-INS-01): `https://raw.githubusercontent.com/quirq-ai/release/release-state/channels.json`.

`tools/rollback_drill.py` ships two canaries per repo through the executor, rolls back, and checks
that the ref, the pointer and `channels.json` all name the previous canary again, within 10
minutes. Presubmit runs it on every change.

## The daily canary (V0-REL-03)

The `canary` workflow runs daily at the canary schedule in infra-config `channels.toml` (off the
hour), for every repo with a canary release builder in `pipelines.toml`. Stages, in order; the
first failure holds the canary and the previous one stays in place:

| Stage | What | Where |
|---|---|---|
| select | lkgr's commit. Already the canary, held before, or no lkgr yet: a recorded no-op | `plan` job |
| build | every target through its adapter (recipes, pinned); the artifact digest names the build actions and their outputs | `stages` job, read-only |
| verify | the full test suites | `stages` |
| fuzz smoke | property tests only (until V1-REL-02 adds fuzzers): the suites again with 1,000 examples and a daily seed | `stages` |
| deploy, probe | start the artifact in the canary test environment (recipes' deploy; in v0 the runner) and probe it. Every `health.toml` probe for the repo must have run and passed: a probe that did not run is a missing signal, so the canary is held | `stages` |
| promote | the executor moves `channels/canary` to the commit and digest, recording the operation key first | `finish` job, under the `release-channels` lock |

Every run leaves `canary/<repo>/runs/<date>.json` (schema `qq-canary-run/1`) on `release-state`, and
a held commit leaves `canary/<repo>/held/<commit>.json` so it is never retried; the next canary
waits for lkgr to move. Each held canary gets a failure record mirrored to a `qq-failure` issue
(test-pipelines' `failure` action, V0-TST-04) and a postmortem draft issue labelled `postmortem`
from infra-config's template (trigger `canary-deploy-failed`); v0 fills it from captured evidence,
and an agent completes it in v1. Stage results go to the results store through the sink (run kind
`canary`).

Only a stage's verdict holds a commit. `finish` recomputes it from the stage results itself (all four
stages, in order, passing, with a `sha256:` digest), since the worker runs product code. When the
pipeline itself fails (a lost worker, missing or incomplete results, a promote that could not be
written), the outcome is `error`: nothing is held, the job goes red, and the day counts as not yet
run. The day's verdict stays on top of its record (a hold always does) and later runs are kept under `later`;
rerunning `finish` after a ship records the ship again, never a hold.

GitHub may drop a scheduled run, so `canary-watchdog` checks twice a day that every canary repo has
a verdict or no-op for today and, if one is missing and no canary is in flight, starts `canary` by hand.

`tests/canary_demo.py` plays eight days against a fixture service (`tests/fixtures/canary_app`): seven
ship with no human touch, and a planted bad canary (its `/health` answers 500) is held at
deploy-probe with the previous canary kept. Presubmit runs it.

Until onboarding lands `infra/repo.toml` in xo-space and innernet (V0-ONB-01) and their post-submit
builders make an lkgr (xo-space #211, innernet #37), each day's record is a no-op saying why.

## The daily canary report (V0-REL-04)

After each day's canary, the `report` job files one issue, `Canary report <date>`, labelled
`canary-report` (a rerun the same day edits it): what shipped, what was held and why, what was a
no-op or did not run, what canary names now, and the open failure records (`qq-failure` issues). The
same text is kept on `release-state` as `reports/<date>.md`. `qqrelease canary report` builds it; the
canary demo checks a report exists for every day and that the bad day's says held.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-REL-01 | `lkgr` ref | #2 | merged |
| V0-REL-02 | Channel pointers and rollback | #3 | merged |
| V0-REL-03 | Daily canary pipeline v0 | #4 | in review |
| V0-REL-04 | Daily canary report | #5 | in review |

Out of scope for v0: soak, automatic rollback, the fuzz stage, the dev channel and PostHog (v1);
stable and staged rollout (v2).

## Working here

See [AGENTS.md](AGENTS.md). Run the checks as CI does: clone infra-config at the `pins.toml` commit into
`.qq/infra-config`, then `python -m pip install -e ".[test]" && python -m pytest`.

## Licence

Apache-2.0; see [LICENSE](LICENSE).
