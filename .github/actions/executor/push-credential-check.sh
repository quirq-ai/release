# Check, from the release-state worktree, that its pushes send exactly the credential
# push-credential.sh installed and nothing else can override it. Prints only who pushes.
set -euo pipefail
key=http.https://github.com/.extraheader
fail() { echo "::error::release-state push credential: $1"; exit 1; }
# Every extraheader git would send to github.com, whatever URL scope it is set under (http.*, a
# host, a repository path): exactly one, the key this job installed.
n=$(git config --get-regexp '^http\..*extraheader$' | grep -c . || true)
[ "$n" = 1 ] || fail "expected exactly one http.*.extraheader (Authorization header), found $n"
got=$(printf '%s' "$(git config --get "$key")" | sha256sum | cut -d' ' -f1)
[ "$got" = "${EXPECTED_SHA256:?}" ] || fail "the header is not the one this job installed"
if git config --get-regexp '^credential\..*helper$' > /dev/null; then fail "a credential helper is configured"; fi
# Fetches and pushes go to this repository's URL exactly as checkout set it, and only there: no
# second URL, url.*.insteadOf, pushInsteadOf, remote push URL or remote helper may redirect either
# (rewrites of other URLs, like a proxy's ssh-to-https, are fine). Transport settings (http proxy,
# sslVerify, curloptResolve, cookieFile) are out of scope here: see the README's v1 list.
expected="${GITHUB_SERVER_URL:?}/${GITHUB_REPOSITORY:?}"
url=$(git config --get-all remote.origin.url) || fail "no origin remote"
[ "$(printf '%s\n' "$url" | wc -l)" = 1 ] || fail "origin has more than one URL"
case "$url" in
  *@*) fail "origin's URL carries credentials" ;;
  "$expected" | "$expected.git") ;;
  *) fail "origin is not $expected" ;;
esac
[ "$(git remote get-url --all origin)" = "$url" ] || fail "a url.*.insteadOf rewrites origin's fetch URL"
[ "$(git remote get-url --push --all origin)" = "$url" ] || fail "a url.*.insteadOf, pushInsteadOf or remote push URL rewrites the push URL"
if git config --get-regexp '^remote\.origin\.vcs$' > /dev/null; then fail "origin goes through a remote helper (remote.origin.vcs)"; fi
if [ -n "${APP_SLUG:-}" ]; then
  echo "release-state pushes as ${APP_SLUG}[bot]"
else
  echo "::warning::release-state pushes as github-actions[bot] (no QQ_RELEASE_CLIENT_ID)"
fi
