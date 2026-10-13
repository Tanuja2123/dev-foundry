import os
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from app.services import conversation_service


SLM_ENV = ("MAQ_AI_API_KEY", "MAQ_AI_BASE_URL", "MAQ_AI_MODEL",
           "LOCAL_SLM_API_KEY", "LOCAL_SLM_APIKEY", "LOCAL_SLM_BASE_URL", "LOCAL_SLM_MODEL")


@pytest.fixture(autouse=True)
def isolated_slm_env(monkeypatch):
    """The real .env must never leak into these tests (MAQ_AI_* takes precedence over LOCAL_SLM_*)."""
    for name in SLM_ENV:
        monkeypatch.delenv(name, raising=False)


def test_get_local_slm_config_defaults(monkeypatch):
    monkeypatch.delenv("LOCAL_SLM_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_SLM_APIKEY", raising=False)
    monkeypatch.delenv("LOCAL_SLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_SLM_MODEL", raising=False)

    config = conversation_service.get_local_slm_config()
    assert config["api_key"] == ""
    assert config["base_url"] == "https://indiaai.maqsoftware.net/v1"   # PR 24 default
    assert config["model"] == "qwen-3.8-27b"


def test_get_local_slm_config_custom(monkeypatch):
    monkeypatch.setenv("LOCAL_SLM_API_KEY", "sk-custom-test-key")
    monkeypatch.setenv("LOCAL_SLM_BASE_URL", "https://custom.slm/v1")
    monkeypatch.setenv("LOCAL_SLM_MODEL", "custom-model")

    config = conversation_service.get_local_slm_config()
    assert config["api_key"] == "sk-custom-test-key"
    assert config["base_url"] == "https://custom.slm/v1"
    assert config["model"] == "custom-model"


def test_get_local_slm_client_missing_key(monkeypatch):
    monkeypatch.delenv("LOCAL_SLM_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_SLM_APIKEY", raising=False)

    with pytest.raises(ValueError, match="MAQ_AI_API_KEY missing"):
        conversation_service.get_local_slm_client()


def test_get_local_slm_client_success(monkeypatch):
    monkeypatch.setenv("LOCAL_SLM_API_KEY", "sk-test-key")
    client = conversation_service.get_local_slm_client()
    assert client is not None


@pytest.mark.asyncio
async def test_generate_chat_response_fallback_to_direct_slm(monkeypatch):
    # Simulate Azure OpenAI failure
    monkeypatch.setattr(
        conversation_service,
        "get_azure_openai_client",
        MagicMock(side_effect=RuntimeError("Azure OpenAI 403 Forbidden")),
    )
    # Set Local SLM credentials
    monkeypatch.setenv("LOCAL_SLM_API_KEY", "sk-test-slm-key")

    # Simulate agent pipeline failing on local SLM as well
    monkeypatch.setattr(
        conversation_service,
        "_execute_agent_pipeline",
        AsyncMock(side_effect=RuntimeError("Agent Framework not supported")),
    )

    # Mock direct OpenAI client chat completion
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Hello from fallback SLM!"))]
    mock_openai_instance = MagicMock()
    mock_openai_instance.chat.completions.create.return_value = mock_resp

    with patch("app.services.conversation_service.OpenAI", return_value=mock_openai_instance):
        response = await conversation_service.generate_chat_response("hi", [])
        assert response == "Hello from fallback SLM!"
        assert mock_openai_instance.chat.completions.create.called
