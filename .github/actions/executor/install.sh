# Install qqrelease with the python on PATH: PyPI packages by hash only (requirements.txt), then the
# qq packages by commit (requirements-qq.txt) and this repo, built without fetching anything else.
# `pip check` fails if something they need is missing. Run from the repository root with
# PYTHONSAFEPATH=1. presubmit runs this too, so a stale hash or a missing package fails there.
set -euo pipefail
here=$(dirname "$0")
python -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r "$here/requirements.txt"
# pip builds `.` in the tree, and setuptools ships whatever build/lib already holds: start empty.
rm -rf build
python -m pip install --quiet --disable-pip-version-check --no-deps --no-build-isolation -r "$here/requirements-qq.txt" .
python -m pip check
