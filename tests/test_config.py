from qqrelease import config


def test_lkgr_ref_comes_from_channels_toml(cfg):
    assert config.lkgr_ref(cfg) == cfg["channels"]["source"]["ref"]


def test_every_onboarded_repo_has_post_submit_builders(cfg):
    repos = config.repos(cfg)
    assert repos and all(r.postsubmit for r in repos)


def test_backend_comes_from_pipelines_toml(cfg):
    assert config.backend_name(cfg) == cfg["pipelines"]["defaults"]["backend"]
    assert config.backend_name(cfg, "local") == "local"
