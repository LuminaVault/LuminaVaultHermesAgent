"""Voice-clip transcription for inbound messages (moved out of ``gateway/run_inbound.py``)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from gateway.run_common import _UNSET

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayInboundTranscriptionMixin:
    """STT enrichment and the once-only echo of pending voice transcripts."""

    async def _enrich_message_with_transcription(
        self, user_text: str, audio_paths: list[str], clip_memo: Optional[dict] = None, *,
        error_sink: Optional[list[str]] = None, channel: Optional[str] = None,
    ) -> tuple[str, list[str]]:
        """Transcribe voice clips with the configured STT provider and prepend the transcripts →
        ``(enriched_text, successful_transcripts)``; the transcripts (input order; empty if every clip
        failed or STT is disabled) let callers echo them back before the agent loop. ``clip_memo``
        (path → ``_transcribe_one_clip`` result) skips clips an earlier pass already transcribed.

        LuminaVault: ``error_sink``, when given, collects the ``error_kind`` of each failed clip
        (``tools.transcription_tools.classify_stt_status``) so the caller can tell the user about an
        actionable cause out of band; the LLM only ever sees the neutral marker. It is opt-in via a
        mutable argument so existing callers are unchanged, and per call because sessions transcribe
        concurrently. ``channel`` is the platform the clips arrived on, forwarded to STT for metering
        attribution; advisory only."""
        from gateway.run import _probe_audio_duration
        audio_paths = list(dict.fromkeys(audio_paths))
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                suffix = f" (duration: {duration_str})" if duration_str else ""
                notes.append(f"[The user sent a voice message: {abs_path}{suffix}]")
            return (self._prepend_media_prefix("\n\n".join(notes), user_text) if notes else user_text), []

        try:
            from tools.transcription_tools import (
                transcribe_audio, transcribe_audio_local_fallback
            )
        except ModuleNotFoundError as e:
            logger.error("Transcription module unavailable: %s", e)
            return self._prepend_media_prefix("[voice message could not be transcribed]", user_text), []

        enriched_parts = []
        successful_transcripts: list[str] = []
        for path in audio_paths:
            try:
                if clip_memo is not None and path in clip_memo:
                    transcript, note = clip_memo[path]
                else:
                    logger.debug("Transcribing user voice: %s", path)
                    transcript, note = await self._transcribe_one_clip(
                        path, transcribe_audio, transcribe_audio_local_fallback,
                        error_sink=error_sink, channel=channel,
                    )
                    if clip_memo is not None:
                        clip_memo[path] = (transcript, note)
                if transcript is not None:
                    successful_transcripts.append(transcript)
                enriched_parts.append(note)
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append(self._untranscribed_audio_note(path))

        if enriched_parts:
            user_text = self._prepend_media_prefix("\n\n".join(enriched_parts), user_text)
        return user_text, successful_transcripts

    def _pending_event_audio_paths(self, event) -> list[str]:
        """Return STT-eligible paths from a pending voice message."""
        from gateway.run import _event_media_is_stt_input
        return [
            path for i, path in enumerate(getattr(event, "media_urls", None) or [])
            if _event_media_is_stt_input(event, i)
        ]

    async def _transcribe_pending_audio_event_once(
        self, event, user_text: Optional[str] = None
    ) -> tuple[str | None, list[str]]:
        """Transcribe a pending audio event once and cache the result on the event: the interrupt
        monitor and the pending-drain path both need it — one STT call and one echo per message.
        ``user_text`` is a deliberate override of the caption; ``None`` reads ``event.text`` live."""
        if hasattr(event, "_gateway_pending_stt_text"):
            return event._gateway_pending_stt_text, list(getattr(event, "_gateway_pending_stt_transcripts", []) or [])
        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            return user_text if user_text is not None else (getattr(event, "text", None) or None), []
        text = user_text if user_text is not None else (getattr(event, "text", "") or "")
        # Per-clip results outlive the merged-event cache below (a merge drops only that), so every
        # re-run — this loop's or the drain path's — transcribes just the newly merged clips.
        clip_memo = getattr(event, "_gateway_pending_stt_clips", None)
        if clip_memo is None:
            clip_memo = {}
            event._gateway_pending_stt_clips = clip_memo
        channel = self._stt_channel(getattr(event, "source", None))
        enriched_text, successful_transcripts = await self._enrich_message_with_transcription(
            text, audio_paths, clip_memo, channel=channel,
        )
        # A voice note merged in during the await dropped a cache that did not exist yet; re-run on
        # the merged media rather than publish a transcript that silently omits the new note.
        while self._pending_event_audio_paths(event) != audio_paths:
            audio_paths = self._pending_event_audio_paths(event)
            text = user_text if user_text is not None else (getattr(event, "text", "") or "")
            enriched_text, successful_transcripts = await self._enrich_message_with_transcription(
                text, audio_paths, clip_memo, channel=channel,
            )
        event._gateway_pending_stt_text = enriched_text
        event._gateway_pending_stt_transcripts = list(successful_transcripts)
        return enriched_text, successful_transcripts

    async def _echo_pending_stt_transcripts_once(
        self, event, adapter, source, transcripts: list[str], *, metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        """Echo pending-event STT transcripts to the chat at most once. Tracked as a COUNT (not a
        set — identical transcripts are distinct deliveries): ``merge_pending_message_event`` can
        append a second voice note and invalidate the cache; the re-run returns earlier transcripts
        as a prefix, so only the unsent tail is echoed."""
        if not transcripts or not self._should_echo_stt_transcripts() or adapter is None:
            return
        already_echoed = int(getattr(event, "_gateway_pending_stt_echoed", 0) or 0)
        event._gateway_pending_stt_echoed = max(already_echoed, len(transcripts))
        await self._echo_stt_transcripts(
            adapter, source, transcripts[already_echoed:], metadata=metadata, log_context=log_context,
        )

    async def _transcribe_and_echo_pending_voice(
        self, event, adapter, source, text: str, *, log_context: str, metadata=_UNSET
    ) -> tuple[str, list[str]]:
        """Transcribe a pending voice event and echo transcripts once → ``(enriched_text,
        transcripts)`` for ``agent.interrupt()`` or the pending-drain flow; ``(text, [])`` when there
        is no STT-eligible media (caller owns the ``_build_media_placeholder`` fallback). ``text`` is
        only that fallback: the caption is read live from ``event.text`` so one merged in during
        the STT await is kept."""
        if not self._pending_event_audio_paths(event):
            return text, []
        try:
            enriched_text, transcripts = await self._transcribe_pending_audio_event_once(event)
            if metadata is _UNSET:
                metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
            await self._echo_pending_stt_transcripts_once(
                event, adapter, source, transcripts, metadata=metadata, log_context=log_context
            )
            return enriched_text or text, transcripts
        except Exception as trans_exc:
            logger.warning("%s transcription failed: %s", log_context, trans_exc)
            return text, []

    # --- LuminaVault: voice failure notices and STT channel attribution ---------------------------

    #: How long to stay quiet after warning a chat that voice is unavailable. A user out of quota
    #: often sends several voice notes before reading the reply; one warning is information, five
    #: is noise.
    _VOICE_NOTICE_COOLDOWN_SECONDS = 600

    async def _notify_voice_unavailable(
        self, event, source, error_kinds, direction: str = "stt",
    ) -> None:
        """Send one localized notice when voice failed for an actionable reason.

        ``direction`` is ``"stt"`` for inbound transcription or ``"tts"`` for spoken replies; it
        selects the message and scopes the cooldown, keyed ``<direction>:platform:chat_id`` (inbound
        and outbound fail for different reasons, so a user told about one still hears about the
        other). No-op when every failure was transient, when the chat was warned recently, or when
        the platform adapter is unavailable. Never raises: a failed notice must not take down the
        message that triggered it."""
        if not error_kinds:
            return
        try:
            from tools.tool_backend_helpers import (
                AUDIO_ERROR_KIND_AUTH, AUDIO_ERROR_KIND_QUOTA, AUDIO_ERROR_KIND_RATE_LIMIT,
                AUDIO_USER_FACING_ERROR_KINDS)
        except ImportError:
            return
        # Quota outranks rate-limit outranks auth: report the most actionable.
        kind = next((k for k in (AUDIO_ERROR_KIND_QUOTA, AUDIO_ERROR_KIND_RATE_LIMIT, AUDIO_ERROR_KIND_AUTH)
                     if k in error_kinds), None)
        if kind is None or kind not in AUDIO_USER_FACING_ERROR_KINDS:
            return
        sent_at = self.__dict__.setdefault("_voice_notice_sent_at", {})
        chat_key = f"{direction}:{getattr(source, 'platform', None)}:{getattr(source, 'chat_id', None)}"
        now = time.monotonic()
        last = sent_at.get(chat_key)
        if last is not None and (now - last) < self._VOICE_NOTICE_COOLDOWN_SECONDS:
            return
        from agent.i18n import t
        suffix = "" if direction == "stt" else "_reply"
        message = t(f"gateway.voice.unavailable{suffix}_{kind}")
        try:
            adapter = self._adapter_for_source(source)
            if adapter is None:
                return
            metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
            await adapter.send(source.chat_id, message, metadata=metadata)
            sent_at[chat_key] = now
        except Exception as exc:
            logger.debug("Voice unavailable notice failed (non-fatal): %s", exc)

    @staticmethod
    def _stt_channel(source: Any) -> Optional[str]:
        """Name the platform a clip arrived on, for STT metering attribution. Accepts anything with
        ``.platform`` (a ``SessionSource`` or a ``MessageEvent``); None rather than raising when
        unknown, since a voice note must still be transcribed when we cannot name its origin."""
        platform = getattr(source, "platform", None)
        if platform is None:
            return None
        value = getattr(platform, "value", platform)
        return str(value) or None

    _EMPTY_TEXT_PLACEHOLDER = "(The user sent a message with no text content)"

    @classmethod
    def _prepend_media_prefix(cls, prefix: str, user_text: str) -> str:
        """``prefix`` + the user's text; the Discord empty-content placeholder is dropped as redundant."""
        if user_text and user_text.strip() != cls._EMPTY_TEXT_PLACEHOLDER:
            return f"{prefix}\n\n{user_text}"
        return prefix

    @staticmethod
    def _untranscribed_audio_note(path: str) -> str:
        """One minimal neutral marker for every STT failure. Never mention "no STT provider" or setup
        steps — persisted in history they make the model keep volunteering STT-setup advice."""
        from tools.credential_files import to_agent_visible_cache_path
        agent_path = to_agent_visible_cache_path(os.path.abspath(path))
        return f"[voice message could not be transcribed automatically; the audio is available at: {agent_path}]"

    async def _transcribe_one_clip(
        self, path: str, transcribe_audio, transcribe_audio_local_fallback, *,
        error_sink: Optional[list[str]] = None, channel: Optional[str] = None,
    ) -> tuple[Optional[str], str]:
        """``(transcript_or_None, note)`` for one clip via configured STT with local fallback."""
        if channel:  # LuminaVault: only when known, so a transcribe_audio stand-in without it still works
            result = await asyncio.to_thread(transcribe_audio, path, None, "gateway", channel=channel)
        else:
            result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        error_kind = None if result.get("success") else result.get("error_kind")
        if not result.get("success"):
            fallback = await asyncio.to_thread(transcribe_audio_local_fallback, path)
            if fallback.get("success"):
                logger.info("Configured STT failed for %s; recovered with local STT", path)
                result = fallback
        if not result["success"]:
            logger.info("Voice transcription failed for %s: %s", path, result.get("error", "unknown error"))
            # The marker is all the LLM sees; the reason goes to the caller (LuminaVault).
            if error_sink is not None and error_kind:
                error_sink.append(error_kind)
            return None, self._untranscribed_audio_note(path)
        transcript = result["transcript"]
        # STT may return success=True with an empty/whitespace transcript (silence, cut-off);
        # empty quotes make the agent reply to nothing and can loop, so emit a sentinel note.
        # See #41603.
        if not (transcript or "").strip():
            return None, (
                "[The user sent a voice message but it came through "
                "empty or inaudible — speech-to-text returned no "
                "words. Do not guess at the content; ask the user "
                "to resend or type it out.]"
            )
        # Plain quoted line: a "The user sent a voice message..." wrapper read as a meta-instruction
        # and made the LLM comment on voice mode instead.
        return transcript, f'"{transcript}"'
