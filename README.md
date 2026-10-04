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

`release-state` is the trust root the installer reads, and today it is only as safe as push access
to this repo. Executor jobs refuse to run from any branch but `main`, which stops a dispatch from a
branch by accident, but anyone who can push a branch can change that check. **TODO(suraj):** the
real guard is a ruleset on `release-state` whose only bypass is the release executor identity.
That needs two steps in order: (1) create the identity, (2) have the executor jobs push
`release-state` with its token instead of the job's `GITHUB_TOKEN` (today they use the latter, which
a ruleset cannot tell apart from any other workflow here), then apply the ruleset. Until then the
installer should also check that `generation` never goes down.

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
  again. Retrying a rollback whose write already landed does nothing more. `--from` is required: the
  full commit the channel names now, so a re-run or a second dispatch is refused instead of rolling
  back twice. The `channel-rollback` workflow runs it on demand.
- A promotion that changes nothing is refused. When a channel takes its build from another channel
  (dev from canary), it takes that channel's digest too, so only a vetted artifact moves on.
- v0 promotes only channels whose `channels.toml` rules need no person and no signal it cannot read
  yet (canary). A channel that needs an approval, a soak or health signals (dev, stable) is refused,
  and so is a rollback whose `[channel.rollback]` needs an approval: held, never done unchecked.
- After every move, `channels.json` on `release-state` (schema `qq-channels/1`) says what each
  repo's channels name: commit, digest, generation, operation and time. The installer reads it
  (V0-INS-01). Read it from
  `https://raw.githubusercontent.com/quirq-ai/release/refs/heads/release-state/channels.json`: the
  `refs/heads/` form means a tag named `release-state` can never be served instead. (The installer's
  own default still uses the bare name; that change belongs to the installer.)

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
a held commit leaves `canary/<repo>/held/<commit>.json` so it is not retried; the next canary
waits for lkgr to move, or for the hold to be released (below). Each held canary gets a failure record mirrored to a `qq-failure` issue
(test-pipelines' `failure` action, V0-TST-04) and a postmortem draft issue labelled `postmortem`
from infra-config's template (trigger `canary-deploy-failed`); v0 fills it from captured evidence,
and an agent completes it in v1. Stage results go to the results store through the sink (run kind
`canary`).

Only a stage's verdict holds a commit. `finish` recomputes it from the stage results itself (all four
stages, in order, passing, with a `sha256:` digest), since the worker runs product code. When the
pipeline itself fails (a lost worker, missing or incomplete results, a stage whose adapter could not
run: a timeout, a crash, no results, judged by exit codes alone; a promote that could not be
written), the outcome is `error`: nothing is held, the job goes red, and the day counts as not yet
run. The day's verdict stays on top of its record (a hold always does) and later runs are kept under `later`;
rerunning `finish` after a ship records the ship again, never a hold.

An action that could not start on the machine is `error` too: exit 126 or 127 (the runner records
127 when a command is missing), or the adapter's `fetch:toolchain-check` failing (the machine lacks
the pinned toolchain). These are exit codes and action names qqrecipes writes, never output text.
Product code can exit 126 or 127 too, so an error is never final: a commit the canary could not
judge in 3 canary runs since its last release (a re-run of the same run's finish counts once) (`ERROR_LIMIT`; about a day of the schedule plus the
watchdog) is held, tagged `possible runner fault`, with a failure record and a postmortem draft like
any hold. Until then it is rerun; a flaky failure can still ship on a green rerun within those 3.
The same cap ends the loop for a commit that breaks its own `infra/repo.toml` (qqrecipes stops
before writing results), for stage results of any malformed shape, and for a lost worker. So a
runner outage of about a day (or three cancelled canary runs) holds every canary repo's current
lkgr commit as a possible runner fault, each with its own failure issue: nothing ships, and each
repo recovers when lkgr moves or its hold is released. A promote that fails after every stage
passed is not counted: the commit is not in doubt, the executor is.

A machine fault that looks like an ordinary failing action is held too. Once the machine is fixed,
release the hold with the `canary-release-hold` workflow (repo, full held commit, `released_before`,
reason), which runs `qqrelease canary release-hold`. `released_before` names the hold: the number of
entries in the hold record's `releases` (0 for the commit's first hold). From main only, it records
a `release-hold` operation, keyed on the release before it, and marks the hold record released in one
release-state commit, with the run and who dispatched it; the next canary builds the commit again.
A retried dispatch is a no-op, and re-running a finished release after the commit was held again is
refused, so each hold needs its own release. (Releases recorded before this chaining, at
`cd88d72`, are not chained and are refused as hand edits; release-state had no canary records then.) The reason is one line of at most 500 characters. Close the
hold's failure issue by hand with what was wrong with the machine.

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
| V0-REL-03 | Daily canary pipeline v0 | #4 | merged |
| V0-REL-04 | Daily canary report | #6 | merged |

Audit fixes after v0: B1 (#8), S1-S3 (#9), S4 (#10), S5-S6 (#11), S7-S8 (#12); audits cleared at
`71a987f`.

Out of scope for v0: soak, automatic rollback, the fuzz stage, the dev channel and PostHog (v1);
stable and staged rollout (v2).

### Open for v1 (non-blocking findings from the v0 audits and reviews)

Waiting on the release executor identity and the `release-state` ruleset (suraj, with the post-v0
bots design):

- The release chain is a consistency check, not authentication, and `is_held` trusts `state` alone.
- A rebase after an admin force-push of `release-state` could replay commits the rewind removed; the
  ruleset's no-force-push rule closes it.

The state store:

- A push refused for a reason other than a race is retried 4 times as if it were one, and the error
  does not say it was not one.
- `_reset_to_branch` falls back to `HEAD~1` when the fetch fails: silent on a root commit, and a
  publish that landed is then reported as failed (the safe side).
- `save` writes files before `git add`/`commit`, outside the reset; a failure there leaves the
  worktree dirty for later reads in the same process.
- The compare-and-swap covers writes only: an input a writer only reads (the error count's
  generation, from the hold record) can be stale. Harmless today.
- A non-UTF-8 path from another writer refuses racing publishes within the race window.

The canary:

- The `finish` fallback with no stage results, after a promote that landed, records `error` and
  counts it; nothing ships, but the day reads error instead of shipped.
- A promote bug that fails every run turns the job red daily with no cap or escalation.
- A forged `skip` gives an uncounted daily no-op (the accepted worker-trust model), and every repo's
  stage results share one artifact namespace within a run.
- About a day of runner outage holds every canary repo as a possible runner fault (documented above).
- A release reason keeps non-whitespace control characters.
- Held and report issue bodies can exceed GitHub's 65,536-character limit.
- The `earlier`/`later` cap reuses `RUN_IDS_KEPT`; give it its own constant.

## Working here

See [AGENTS.md](AGENTS.md). Run the checks as CI does: clone infra-config at the `pins.toml` commit into
`.qq/infra-config`, then `python -m pip install -e ".[test]" && python -m pytest`.

## Licence

Apache-2.0; see [LICENSE](LICENSE).
