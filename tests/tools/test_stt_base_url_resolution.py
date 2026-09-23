"""Regression tests for ``stt.openai.base_url`` on the env-key path.

``_resolve_openai_audio_client_config`` has two ways to find an OpenAI-shaped
STT credential: ``stt.openai.api_key`` in config.yaml, or the
``VOICE_TOOLS_OPENAI_KEY`` / ``OPENAI_API_KEY`` environment variables. Only the
first used to honour a configured ``base_url``; the env path hard-coded
``OPENAI_BASE_URL``.

That asymmetry is a live footgun for any deployment that keeps the endpoint in
config.yaml and the secret in the environment (the shape managed LuminaVault
tenants use): the credential for a private audio proxy was sent to
``api.openai.com``, which answers 401, and the user saw only
"voice message could not be transcribed".

``tools/tts_tool.py`` never had the bug — these tests pin the STT side to match.
"""

from unittest.mock import patch

import pytest


PROXY = "https://api.example.test/v1"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Drop the direct-key env vars so each test states its own credential."""
    for key in ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def _resolve(stt_config, direct_key=""):
    from tools import transcription_tools as tt

    with patch.object(tt, "_load_stt_config", return_value=stt_config), \
         patch.object(tt, "resolve_openai_audio_api_key", return_value=direct_key):
        return tt._resolve_openai_audio_client_config()


class TestBaseURLResolution:
    def test_env_key_honours_configured_base_url(self):
        """The regression. Key from the environment, endpoint from config.yaml."""
        api_key, base_url = _resolve(
            {"openai": {"base_url": PROXY}},
            direct_key="env-secret",
        )
        assert api_key == "env-secret"
        assert base_url == PROXY

    def test_config_key_honours_configured_base_url(self):
        """The path that always worked — pinned so it cannot regress."""
        api_key, base_url = _resolve(
            {"openai": {"api_key": "cfg-secret", "base_url": PROXY}},
        )
        assert api_key == "cfg-secret"
        assert base_url == PROXY

    def test_env_key_without_configured_base_url_falls_back_to_openai(self):
        """No base_url configured must still reach OpenAI proper."""
        from tools import transcription_tools as tt

        api_key, base_url = _resolve({"openai": {}}, direct_key="env-secret")
        assert api_key == "env-secret"
        assert base_url == tt.OPENAI_BASE_URL

    def test_empty_stt_config_falls_back_to_openai(self):
        """No ``stt`` section at all is the default install."""
        from tools import transcription_tools as tt

        api_key, base_url = _resolve({}, direct_key="env-secret")
        assert api_key == "env-secret"
        assert base_url == tt.OPENAI_BASE_URL

    def test_config_key_wins_over_env_key(self):
        """Explicit config beats ambient environment, and keeps its base_url."""
        api_key, base_url = _resolve(
            {"openai": {"api_key": "cfg-secret", "base_url": PROXY}},
            direct_key="env-secret",
        )
        assert api_key == "cfg-secret"
        assert base_url == PROXY
