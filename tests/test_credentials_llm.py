from unittest import mock

import pytest

from lucidfish import credentials
from lucidfish.config import LLMConfig
from lucidfish.llm import LLMError, _claude_version, clean_output, make_provider


def test_key_lookup_order(monkeypatch):
    assert not credentials.get_key("openai", "OPENAI_API_KEY").present
    assert credentials.set_key("openai", "sk-session-1234567") == "session"   # no keychain in tests
    info = credentials.get_key("openai", "OPENAI_API_KEY")
    assert info.source == "session" and info.key == "sk-session-1234567"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-7654321")
    assert credentials.get_key("openai", "OPENAI_API_KEY").source == "env"
    credentials.delete_key("openai")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert not credentials.get_key("openai", "OPENAI_API_KEY").present


def test_public_key_info_never_contains_the_key():
    credentials.set_key("groq", "gsk_abcdefghijklmnop")
    public = credentials.get_key("groq", "GROQ_API_KEY").public()
    assert "gsk_abcdefghijklmnop" not in str(public)
    assert public["hint"] == "gsk…mnop"


def test_validate_and_redact():
    with pytest.raises(ValueError):
        credentials.validate_key("has spaces in it")
    assert credentials.redact("bad key sk-secret-123456", "sk-secret-123456") == "bad key sk-…3456"


def test_claude_model_parsing():
    assert _claude_version("claude-opus-4-6") == ("opus", 4.6)
    assert _claude_version("claude-haiku-4-5-20251001") == ("haiku", 4.5)
    assert _claude_version("claude-sonnet-5") == ("sonnet", 5.0)
    assert _claude_version("claude-3-5-haiku-latest") == ("haiku", 3.5)
    assert _claude_version("gpt-4o") is None


def test_clean_output_strips_reasoning():
    assert clean_output("<think>hmm</think>\nEXPLANATION: ok") == "EXPLANATION: ok"


def test_cloud_provider_requires_key():
    with pytest.raises(LLMError) as err:
        make_provider(LLMConfig(provider="openai"))
    assert err.value.fatal and "OPENAI_API_KEY" in str(err.value)


def _response(status, payload):
    r = mock.Mock(status_code=status, headers={})
    r.json.return_value = payload
    r.text = str(payload)
    return r


def test_openai_compatible_errors_are_friendly_and_redacted():
    provider = make_provider(LLMConfig(provider="openai", api_key="sk-live-abcdefgh1234"))
    with mock.patch("requests.Session.post", return_value=_response(
            401, {"error": {"message": "Incorrect API key provided: sk-live-abcdefgh1234"}})):
        with pytest.raises(LLMError) as err:
            provider.generate("sys", "hi")
    assert err.value.fatal and "rejected the API key" in str(err.value)
    assert "sk-live-abcdefgh1234" not in str(err.value)


def test_openai_compatible_retries_without_temperature():
    provider = make_provider(LLMConfig(provider="openai", api_key="sk-live-abcdefgh1234"))
    responses = [_response(400, {"error": {"message": "Unsupported value: 'temperature'"}}),
                 _response(200, {"choices": [{"message": {"content": "EXPLANATION: fine"}}]})]
    with mock.patch("requests.Session.post", side_effect=responses) as post:
        assert provider.generate("sys", "hi") == "EXPLANATION: fine"
    assert "temperature" not in post.call_args.kwargs["json"]


def test_ollama_missing_model_message():
    provider = make_provider(LLMConfig(provider="ollama", model="nope:1b"))
    with mock.patch("requests.Session.post", return_value=_response(404, {"error": "model 'nope:1b' not found"})):
        with pytest.raises(LLMError) as err:
            provider.generate("sys", "hi")
    assert err.value.fatal and "ollama pull nope:1b" in str(err.value)
