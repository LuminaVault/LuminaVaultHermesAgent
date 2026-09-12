"""Tell the managed audio gateway which channel a voice note came from.

The server meters every transcription, but over the OpenAI wire shape it can
only see the tenant — a Telegram voice note and an iOS mic recording arrive
identical. Without attribution, "how much does Telegram voice cost us" is not
a question the usage rows can answer.

So the channel travels as request headers on the OpenAI client. Headers rather
than a form field because the server must stay OpenAI-shaped for every other
client, and an unknown *form* field would have to be either rejected or
silently ignored — neither is a good default for a metering signal.

The headers are advisory: a missing or unknown channel is recorded as
``unknown`` server-side, never an error. Transcription must never fail because
attribution failed.
"""

import struct
import sys
import types
import wave
from unittest.mock import MagicMock, patch

import pytest

if "faster_whisper" not in sys.modules:
    from importlib.machinery import ModuleSpec

    faster_whisper_stub = types.ModuleType("faster_whisper")
    faster_whisper_stub.WhisperModel = MagicMock(name="WhisperModel")
    faster_whisper_stub.__spec__ = ModuleSpec("faster_whisper", loader=None)
    sys.modules["faster_whisper"] = faster_whisper_stub


pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")


@pytest.fixture
def sample_wav(tmp_path):
    """A minimal valid WAV file (1 second of silence at 16kHz)."""
    wav_path = tmp_path / "test.wav"
    n_frames = 16000
    silence = struct.pack(f"<{n_frames}h", *([0] * n_frames))

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(silence)

    return str(wav_path)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def _openai_client():
    client = MagicMock()
    client.audio.transcriptions.create.return_value = "hello"
    return client


class TestTranscribeOpenAIHeaders:
    def test_channel_travels_as_a_request_header(self, monkeypatch, sample_wav):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=_openai_client()) as openai_cls:
            from tools.transcription_tools import _transcribe_openai
            _transcribe_openai(sample_wav, "whisper-1", channel="telegram")

        headers = openai_cls.call_args.kwargs["default_headers"]
        assert headers["X-Lumina-Channel"] == "telegram"
        assert headers["X-Lumina-Surface"] == "voice_note"

    def test_no_channel_sends_no_channel_header(self, monkeypatch, sample_wav):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=_openai_client()) as openai_cls:
            from tools.transcription_tools import _transcribe_openai
            _transcribe_openai(sample_wav, "whisper-1")

        headers = openai_cls.call_args.kwargs.get("default_headers") or {}
        assert "X-Lumina-Channel" not in headers

    def test_channel_is_normalised_to_a_bare_token(self, monkeypatch, sample_wav):
        """Header values must survive the wire: no newlines, no exotic casing.

        A platform name reaches here from plugin platforms too, where it is
        whatever string the plugin chose. An httpx client raises on a header
        value containing a newline, which would turn a metering nicety into a
        failed transcription.
        """
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=_openai_client()) as openai_cls:
            from tools.transcription_tools import _transcribe_openai
            _transcribe_openai(sample_wav, "whisper-1", channel="Telegram\nX-Evil: 1")

        headers = openai_cls.call_args.kwargs["default_headers"]
        assert headers["X-Lumina-Channel"] == "telegram"


class TestTranscribeAudioForwardsChannel:
    def test_transcribe_audio_passes_channel_through(self, monkeypatch, sample_wav):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")

        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "openai"},
        ), patch(
            "tools.transcription_tools._get_provider", return_value="openai"
        ), patch(
            "tools.transcription_tools._transcribe_openai",
            return_value={"success": True, "transcript": "hi", "provider": "openai"},
        ) as transcribe_openai:
            from tools.transcription_tools import transcribe_audio
            transcribe_audio(sample_wav, model="whisper-1", channel="telegram")

        assert transcribe_openai.call_args.kwargs["channel"] == "telegram"


class TestEveryTranscriptionPathIsAttributed:
    def test_no_call_site_omits_the_channel(self):
        """Every gateway transcription entry point must name its platform.

        There are four of them — the main inbound path plus three batching /
        voice-interrupt paths — and they are far apart in a very large file.
        A fifth added later without ``channel=`` would silently book its
        traffic as ``unknown``, which looks like working code and reads as a
        drop in Telegram usage. Assert the invariant at the source, since
        exercising all four paths end-to-end costs more than it proves.
        """
        import ast
        from pathlib import Path

        import gateway.run

        tree = ast.parse(Path(gateway.run.__file__).read_text())
        call_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_enrich_message_with_transcription"
        ]

        assert len(call_sites) == 4, "call-site count changed — update this test"
        unattributed = [
            node.lineno
            for node in call_sites
            if not any(kw.arg == "channel" for kw in node.keywords)
        ]
        assert unattributed == [], (
            f"transcription call sites without channel= at lines {unattributed}"
        )


class TestGatewayAttributesTheChannel:
    @pytest.mark.asyncio
    async def test_enrichment_forwards_the_platform_as_the_channel(self):
        from gateway.config import GatewayConfig
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(stt_enabled=True)
        runner._voice_notice_sent_at = {}

        transcribe = MagicMock(return_value={"success": True, "transcript": "hi"})
        with patch("tools.transcription_tools.transcribe_audio", transcribe):
            await runner._enrich_message_with_transcription(
                "", ["/tmp/a.ogg"], channel="telegram",
            )

        assert transcribe.call_args.kwargs["channel"] == "telegram"

    @pytest.mark.asyncio
    async def test_enrichment_without_a_channel_still_transcribes(self):
        """Every existing call site omits the channel. None of them may break."""
        from gateway.config import GatewayConfig
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(stt_enabled=True)
        runner._voice_notice_sent_at = {}

        transcribe = MagicMock(return_value={"success": True, "transcript": "hi"})
        with patch("tools.transcription_tools.transcribe_audio", transcribe):
            text, transcripts = await runner._enrich_message_with_transcription(
                "", ["/tmp/a.ogg"],
            )

        assert transcripts == ["hi"]
