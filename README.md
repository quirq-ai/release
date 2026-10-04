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

- Rules come from infra-config's `channels.toml` and `health.toml`, read through `qqcfg` at the
  commit pinned in `pins.toml`. Changing them is a policy change for suraj.
- Record an operation key before every external effect, so a retry never acts twice.
- A missing signal means hold, never promote.
- Schedules can be dropped: run off the hour, with a watchdog for missed runs.
- GitHub-specific code sits behind the `backend` field (`github` now, `launchpad` later).

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-REL-01 | `lkgr` ref | | not started |
| V0-REL-02 | Channel pointers and rollback | | not started |
| V0-REL-03 | Daily canary pipeline v0 | | waits on V0-TST-04 |
| V0-REL-04 | Daily canary report | | waits on V0-REL-03 |

Out of scope for v0: soak, automatic rollback, the fuzz stage, the dev channel and PostHog (v1);
stable and staged rollout (v2).

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
