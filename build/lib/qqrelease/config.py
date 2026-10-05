"""infra-config, read through its own loader and validator at the commit pinned in `pins.toml`.

release parses no config itself: the gardener's reader (qqgarden, pinned by commit) imports
`tools/qqcfg.py` from the checkout, refuses a config that fails `qqcfg validate`, and returns
`qqcfg.load`. Which builders a commit must pass is the same definition the gardener uses for the
tree status: the repo's post-submit builders triggered by `land`.
"""
from __future__ import annotations

from pathlib import Path

from qqgarden import config as garden_config
from qqgarden.errors import GardenerError

from qqrelease.errors import ReleaseError

Repo = garden_config.Repo


def load(root: Path) -> dict:
    try:
        return garden_config.load(Path(root))
    except GardenerError as e:
        raise ReleaseError(str(e)) from None


def repos(cfg: dict, only: list[str] | None = None) -> list[Repo]:
    out = garden_config.repos(cfg)
    if only:
        unknown = set(only) - {r.name for r in out}
        if unknown:
            raise ReleaseError(f"not onboarded in infra-config repos.toml: {', '.join(sorted(unknown))}")
        out = [r for r in out if r.name in only]
    return out


def backend_name(cfg: dict, override: str | None = None) -> str:
    name = override or cfg.get("pipelines", {}).get("defaults", {}).get("backend", "")
    if not name:
        raise ReleaseError("no backend: pass --backend or set pipelines.toml [defaults] backend")
    return name


def lkgr_ref(cfg: dict) -> str:
    """The ref channels are cut from (channels.toml [source] ref)."""
    ref = cfg.get("channels", {}).get("source", {}).get("ref", "")
    if not ref:
        raise ReleaseError("channels.toml has no [source] ref: release will not guess where lkgr lives")
    return ref
