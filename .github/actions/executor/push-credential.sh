# Install the one credential release-state pushes use: the release executor App's token when
# CLIENT_ID is set (APP_TOKEN, minted for this repo only), else the job's GITHUB_TOKEN (JOB_TOKEN).
# Run from the repository root of a checkout made with persist-credentials: false.
#
# The header goes into a 0600 file under $RUNNER_TEMP that the repository's local config includes,
# so every worktree (store.py pushes from .qq/state) sends it. Written with shell builtins: the
# token is never in a process's arguments, the remote URL or the global config.
set -euo pipefail
key=http.https://github.com/.extraheader
if [ -n "${CLIENT_ID:-}" ]; then
  [ -n "${APP_TOKEN:-}" ] || { echo "::error::QQ_RELEASE_CLIENT_ID is set but no App token was minted"; exit 1; }
  token=$APP_TOKEN
else
  [ -n "${JOB_TOKEN:-}" ] || { echo "::error::no job token to push release-state with"; exit 1; }
  token=$JOB_TOKEN
fi
if git config --get-all "$key" > /dev/null; then
  # A second Authorization header would be sent first, or make the push fail.
  echo "::error::a credential is already configured for github.com: check out writer jobs with persist-credentials: false"
  exit 1
fi
basic=$(printf 'x-access-token:%s' "$token" | base64 -w0)
echo "::add-mask::$basic"
header="AUTHORIZATION: basic $basic"
file=$(mktemp "${RUNNER_TEMP:?}/qq-push-credential-XXXXXX")   # mode 0600
printf '[http "https://github.com/"]\n\textraheader = %s\n' "$header" > "$file"
git config --local include.path "$file"
echo "header-sha256=$(printf '%s' "$header" | sha256sum | cut -d' ' -f1)" >> "${GITHUB_OUTPUT:?}"
