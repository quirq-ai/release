# Check, from the release-state worktree, that its pushes send exactly the credential
# push-credential.sh installed and nothing else can override it. Prints only who pushes.
set -euo pipefail
key=http.https://github.com/.extraheader
fail() { echo "::error::release-state push credential: $1"; exit 1; }
# Every extraheader git would send to github.com, whatever URL scope it is set under (http.*, a
# host, a repository path): exactly one, the key this job installed.
n=$(git config --get-regexp '^http\..*extraheader$' | grep -c . || true)
[ "$n" = 1 ] || fail "expected exactly one Authorization header for github.com, found $n"
got=$(printf '%s' "$(git config --get "$key")" | sha256sum | cut -d' ' -f1)
[ "$got" = "${EXPECTED_SHA256:?}" ] || fail "the header is not the one this job installed"
if git config --get-regexp '^credential\..*helper$' > /dev/null; then fail "a credential helper is configured"; fi
if git config --get-regexp '^url\..*\.(push)?insteadof$' > /dev/null; then fail "a url.*.insteadOf rewrite is configured"; fi
if git config --get-regexp '^remote\..*\.pushurl$' > /dev/null; then fail "a remote push URL is configured"; fi
if [ -n "${APP_SLUG:-}" ]; then
  echo "release-state pushes as ${APP_SLUG}[bot]"
else
  echo "::warning::release-state pushes as github-actions[bot] (no QQ_RELEASE_CLIENT_ID)"
fi
