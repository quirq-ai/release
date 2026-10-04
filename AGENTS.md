# Agent guide

How an agent changes this repo safely. Read `README.md` first.

- Every change is a pull request against `main`, titled with its work item id (for example
  `V0-REL-01: ...`). It lands only with the `presubmit` check green.
- release is a core repo: its code names no language, build tool or product repo. Repo facts come
  from infra-config (`repos.toml`, `pipelines.toml`, `channels.toml`, `health.toml`) and manifests.
  GitHub-specific code sits behind the `backend` field, in its own module.
- Every external effect (moving `lkgr` or a channel, a deploy) is an operation whose key is recorded
  before the effect happens. A retry with the same key does nothing twice.
- A missing signal means hold. Never promote on no data.
- Other qq repos are used by pinned commit (`pins.toml`, `pyproject.toml`), never copied.
- Pin GitHub Actions by full commit SHA.
- `.github/CODEOWNERS` names suraj (`@sharmasuraj0123`) as owner of the policy and trust paths,
  including the code privileged workflows run; owner names are his call, so never change them. Leave any other `owners` list empty.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- This repo is public: no secrets, tokens or internal hostnames.
