"""Tell the user when transcription failed for a reason they can act on.

Two rules are in tension here, and both matter:

  1. The *prompt* must stay neutral. A previous version leaked "no STT provider
     configured" into the conversation, which persisted in history and made the
     model volunteer setup advice for every later turn. All the LLM may see is
     ``[voice message could not be transcribed]``.

  2. The *user* must still learn why their voice notes stopped working, when
     the cause is quota, rate limiting or misconfiguration. The model cannot
     tell them — it does not know.

So the reason travels out of band, is localized, is limited to actionable
causes, and is debounced per chat.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig


def _runner():
    """A GatewayRunner with only the collaborators these paths touch."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._stt_notice_sent_at = {}
    runner._adapter_for_source = MagicMock()
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    return runner


def _source(chat_id="chat-1", platform="telegram"):
    source = MagicMock()
    source.chat_id = chat_id
    source.platform = platform
    return source


def _failure(kind):
    return {"success": False, "transcript": "", "error": "nope", "error_kind": kind}


class TestErrorKindPropagation:
    @pytest.mark.asyncio
    async def test_error_kind_reaches_the_sink(self):
        runner = _runner()
        sink = []

        with patch("tools.transcription_tools.transcribe_audio", return_value=_failure("quota")):
            text, transcripts = await runner._enrich_message_with_transcription(
                "", ["/tmp/a.ogg"], error_sink=sink,
            )

        assert sink == ["quota"]
        assert transcripts == []

    @pytest.mark.asyncio
    async def test_prompt_stays_neutral_regardless_of_cause(self):
        """The whole point of the sink: the reason must not reach the model."""
        runner = _runner()
        sink = []

        with patch("tools.transcription_tools.transcribe_audio", return_value=_failure("quota")):
            text, _ = await runner._enrich_message_with_transcription(
                "", ["/tmp/a.ogg"], error_sink=sink,
            )

        assert text == "[voice message could not be transcribed]"
        for leaked in ("quota", "allowance", "402", "rate", "billing"):
            assert leaked not in text.lower()

    @pytest.mark.asyncio
    async def test_sink_is_optional(self):
        """Existing callers pass no sink and must be unaffected."""
        runner = _runner()

        with patch("tools.transcription_tools.transcribe_audio", return_value=_failure("quota")):
            text, transcripts = await runner._enrich_message_with_transcription("", ["/tmp/a.ogg"])

        assert text == "[voice message could not be transcribed]"
        assert transcripts == []

    @pytest.mark.asyncio
    async def test_success_leaves_the_sink_empty(self):
        runner = _runner()
        sink = []
        ok = {"success": True, "transcript": "hello there", "provider": "openai"}

        with patch("tools.transcription_tools.transcribe_audio", return_value=ok):
            text, transcripts = await runner._enrich_message_with_transcription(
                "", ["/tmp/a.ogg"], error_sink=sink,
            )

        assert sink == []
        assert transcripts == ["hello there"]
        assert text == '"hello there"'


class TestUserNotice:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["quota", "rate_limit", "auth"])
    async def test_actionable_causes_notify_once(self, kind):
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(MagicMock(), _source(), [kind])

        adapter.send.assert_awaited_once()
        sent_text = adapter.send.await_args.args[1]
        assert sent_text.strip()
        # Localized, not a raw key.
        assert "gateway.voice" not in sent_text

    @pytest.mark.asyncio
    async def test_transient_upstream_failures_stay_quiet(self):
        """A blip should degrade to the neutral marker, not narrate itself."""
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(MagicMock(), _source(), ["upstream"])

        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_failures_means_no_notice(self):
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(MagicMock(), _source(), [])

        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repeat_failures_are_debounced_per_chat(self):
        """Someone out of quota often sends several notes before reading the reply."""
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter
        event, source = MagicMock(), _source()

        for _ in range(4):
            await runner._notify_stt_unavailable(event, source, ["quota"])

        assert adapter.send.await_count == 1

    @pytest.mark.asyncio
    async def test_debounce_does_not_leak_between_chats(self):
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(MagicMock(), _source("chat-a"), ["quota"])
        await runner._notify_stt_unavailable(MagicMock(), _source("chat-b"), ["quota"])

        assert adapter.send.await_count == 2

    @pytest.mark.asyncio
    async def test_quota_outranks_other_causes(self):
        """With mixed causes, report the one the user can actually act on."""
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(
            MagicMock(), _source(), ["upstream", "auth", "quota"],
        )

        from agent.i18n import t
        assert adapter.send.await_args.args[1] == t("gateway.voice.unavailable_quota")

    @pytest.mark.asyncio
    async def test_send_failure_is_swallowed(self):
        """A failed notice must not take down the message that triggered it."""
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock(side_effect=RuntimeError("telegram is down"))
        runner._adapter_for_source.return_value = adapter

        await runner._notify_stt_unavailable(MagicMock(), _source(), ["quota"])

    @pytest.mark.asyncio
    async def test_missing_adapter_is_not_an_error(self):
        runner = _runner()
        runner._adapter_for_source.return_value = None

        await runner._notify_stt_unavailable(MagicMock(), _source(), ["quota"])

    @pytest.mark.asyncio
    async def test_failed_send_is_not_debounced(self):
        """If the notice never landed, the next failure should try again."""
        runner = _runner()
        adapter = MagicMock()
        adapter.send = AsyncMock(side_effect=RuntimeError("down"))
        runner._adapter_for_source.return_value = adapter
        event, source = MagicMock(), _source()

        await runner._notify_stt_unavailable(event, source, ["quota"])
        adapter.send = AsyncMock()
        await runner._notify_stt_unavailable(event, source, ["quota"])

        adapter.send.assert_awaited_once()


class TestStatusClassification:
    @pytest.mark.parametrize("status,expected", [
        (402, "quota"),
        (429, "rate_limit"),
        (401, "auth"),
        (403, "auth"),
        (500, "upstream"),
        (503, "upstream"),
        (None, "upstream"),
    ])
    def test_http_status_maps_to_error_kind(self, status, expected):
        from tools.transcription_tools import classify_stt_status

        assert classify_stt_status(status) == expected

    def test_only_actionable_kinds_are_user_facing(self):
        from tools.transcription_tools import (
            STT_ERROR_KIND_UPSTREAM,
            STT_USER_FACING_ERROR_KINDS,
        )

        assert STT_ERROR_KIND_UPSTREAM not in STT_USER_FACING_ERROR_KINDS
        assert STT_USER_FACING_ERROR_KINDS == {"quota", "rate_limit", "auth"}
