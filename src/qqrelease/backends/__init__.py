"""Backends: where a pointer's git ref lives, picked by the `backend` field in infra-config
(`pipelines.toml [defaults] backend`); `github` in v0, `launchpad` later. `local` keeps target repos
in local git directories, for tests and rollback drills.

A backend module defines `Mirror` with:

    read_ref(repo, ref) -> str                    # the commit the ref names, "" if it does not exist
    write_ref(repo, ref, expected, new) -> str    # moves it to `new` only from a value in `expected`
                                                  # ("" = absent); "pushed", "already there" or
                                                  # "skipped: <why>"
    actor() -> str                                # who is acting: a run URL, or "local"
"""
from __future__ import annotations

import importlib

from qqrelease.errors import ReleaseError


def load(name: str, **kwargs):
    try:
        module = importlib.import_module(f"qqrelease.backends.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"qqrelease.backends.{name}":
            raise ReleaseError(f"no release backend {name!r}; add qqrelease/backends/{name}.py") from None
        raise
    return module.Mirror(**kwargs)
