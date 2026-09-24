from lucidfish import jobs, settings


def test_second_model_settings():
    cfg = settings.load_config()
    assert cfg.expert is None and cfg.llm.escalate
    settings.save({"provider": "ollama", "model": "qwen2.5:7b", "expert_provider": "anthropic",
                   "expert_model": "claude-haiku-4-5", "escalate": False})
    cfg = settings.load_config()
    assert (cfg.expert.provider, cfg.expert.model) == ("anthropic", "claude-haiku-4-5")
    assert cfg.expert.api_key is None and not cfg.llm.escalate
    public = settings.public_settings()
    assert public["expert_provider"] == "anthropic" and public["expert_active"] and public["escalate"] is False
    # Learned time estimates are kept separately for each combination of models.
    assert jobs.Timings.signature(cfg).endswith("+anthropic:claude-haiku-4-5|standard")
    # Command-line flags win; 'none' switches the second model off.
    assert settings.load_config(expert_provider="none").expert is None
    assert settings.load_config(expert_provider="openai").expert.model == "gpt-4.1-mini"
    # The same model twice is just one model.
    settings.save({"expert_provider": "ollama", "expert_model": "qwen2.5:7b"})
    assert settings.load_config().expert is None
    settings.save({"expert_provider": ""})
    assert settings.load_config().expert is None


def test_second_model_validation():
    import pytest
    with pytest.raises(ValueError):
        settings.validate({"expert_provider": "nope"})
    with pytest.raises(ValueError):
        settings.validate({"expert_model": "x" * 300})
