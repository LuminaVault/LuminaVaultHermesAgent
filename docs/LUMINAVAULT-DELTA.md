# What LuminaVault changed in the vendored Hermes Agent import

This repository is **not a git fork** of `NousResearch/hermes-agent`. It is a
squashed vendored import: there is no upstream remote and no common ancestor
with upstream, which is why CI carries a `check-common-ancestor` guard.

The consequence is that **LuminaVault's own changes to upstream files are
invisible in git history**. Anyone re-importing a newer upstream would silently
delete them. This document is the manifest so that cannot happen, and
[`luminavault-delta.patch`](./luminavault-delta.patch) is the whole delta
against the current base as one diff.

## Base

| Import | Upstream | Date |
|---|---|---|
| `0f5b20a` (first import) | `e4ea0a0e` (≈ 0.18.2) | 2026-07-13 |
| this re-import | **`v0.23.1`** (`3b421578`) | 2026-11-16 |

Why the re-import: **CVE-2026-10221** (injection via `_compress_context`,
remotely triggerable, public exploit) affects every version up to 0.19.0.
Upstream's guard (`_transcript_has_real_user_turn`,
`_validate_summary_user_provenance`) hooks into seams 0.18 does not have, so
porting it by hand was not an option. The companion CVE-2026-10224
(`gateway/platforms/feishu.py`) is fixed upstream too.

`luminavault-delta.patch` is `git diff v0.23.1 <this tree> -- . ':!docs/'`.

## What LuminaVault carries

Upstream split several large modules between 0.18 and 0.23, so the features
below live in different files than they did in the first import. Each is
marked `LuminaVault` in a comment at the change.

### Multimodal ingestion API — load-bearing

`POST /v1/ingestions` analyses a remotely hosted source with the normal agent
toolset and returns `{title, markdown, summary, tags, credibility}`. Upstream
has never had it. The LuminaVault server's durable multimodal ingestion path
calls it; losing it breaks that feature.

- `gateway/platforms/api_server.py`: `_handle_ingestion`, the route, the
  capability flags (`multimodal_ingestion`, `ingestion_api`,
  `ingestion_remote_source_url`, `ingestion_supported_mime_types`,
  `ingestion_max_source_bytes`) and the `ingestion` endpoint entry.
- Tests: `tests/gateway/test_api_server.py`.

### Terminal output on `/v1/runs`

Upstream streams an agent's terminal only over the `tui_gateway` WebSocket,
which the cluster does not run, and `/v1/runs` drops the terminal tool's
command and result. This adds them to the run stream:

- `tool.started` for `terminal`: `command`, `background`
- `tool.completed` for `terminal`: `output` (tail, 16k chars), `exit_code`,
  `output_truncated`
- new event `terminal.output`: `{process_id, chunk}` or
  `{process_id, chunk: "", truncated: true}`

Files: `gateway/platforms/api_run_terminal.py` (new: field builders,
`RunTerminalRouter`, `router_for`), `gateway/platforms/api_server_runs.py`
(event fields, router registered around the `/v1/runs` worker by the run's
approval session key). Tests: `tests/gateway/test_api_run_terminal.py`.

### Cross-profile session views on the API server

`GET /api/instance`, `GET /api/profiles/sessions`,
`GET /api/profiles/{profile}/sessions/{session_id}/messages`: read-only, for the
LuminaVault Agents page. Upstream 0.23 has `/api/profiles/sessions` only on the
dashboard web server, which the cluster does not run.

Files: `hermes_cli/profile_sessions.py` (new), handlers and routes in
`gateway/platforms/api_server.py`. Tests: `tests/gateway/test_api_server_profiles.py`.

### Voice: STT/TTS failure notices, channel attribution, `base_url`

- **Failure classification** (`tools/tool_backend_helpers.py`:
  `classify_audio_status`, `classify_audio_exception`, `AUDIO_ERROR_KIND_*`).
  STT (`tools/transcription_cloud.py`, `_with_openai_client`) and TTS
  (`tools/tts_tool.py`, `_tool_failure(..., classify=True)`, `_ChunkFailed`)
  attach an `error_kind` to failures.
- **User notices** (`gateway/run_inbound_transcription.py`:
  `_notify_voice_unavailable`, wired in `gateway/run_inbound.py` and
  `gateway/run_voice.py`): one localized message when voice failed for an
  actionable reason (quota, rate limit, auth), debounced per chat and direction.
  The LLM still sees only the neutral marker. Locale keys
  `gateway.voice.unavailable_*`, `unavailable_reply_*`, `status_stt_*` in every
  `locales/*.yaml` (English text in `ar`, which upstream added later).
  `/voice status` reports whether transcription is enabled
  (`gateway/slash_commands.py`).
- **Channel attribution**: the OpenAI-shaped STT request carries
  `X-Lumina-Channel` / `X-Lumina-Surface` so the LuminaVault audio proxy can
  meter Telegram voice separately. `transcribe_audio(..., channel=)` sets a
  ContextVar that the `openai` route passes to `_transcribe_openai`; every
  gateway call to `_enrich_message_with_transcription` names the channel
  (asserted by `tests/tools/test_stt_channel_attribution.py`).
- **`stt.openai.base_url` on the env-key path** (`_direct_openai_credentials`):
  upstream still sends an environment key to api.openai.com even when config
  names another endpoint.
- Tests: `tests/gateway/test_voice_quota_surfacing.py`,
  `tests/tools/test_stt_channel_attribution.py`,
  `tests/tools/test_stt_base_url_resolution.py`, and the expected call in
  `tests/gateway/test_telegram_audio_vs_voice.py`.

### Default fallback provider chain

`hermes_cli/config_defaults.py`: `fallback_providers` defaults to two zero-cost
OpenRouter slugs, then a cheap billable one, so a fresh tenant whose primary
provider loses auth still has somewhere to go. Tests:
`tests/hermes_cli/test_default_fallback_chain.py`; `tests/hermes_cli/test_fallback_cmd.py`
starts its configs from an empty chain.

### Repository

- `.github/workflows/luminavault-image.yml` publishes
  `ghcr.io/luminavault/luminavault-hermes-agent:sha-*`.
- `.gitignore`: `!website/src/data/`, which the first import lost to the bare
  `data/` rule.
- `.github/`: upstream runs CI on Blacksmith runners and GitHub's large
  runners, which this organisation does not have, so every job queued forever.
  Labels are mapped to GitHub-hosted ones (`blacksmith-*-ubuntu-2404[-arm]` →
  `ubuntu-24.04[-arm]`, `*-windows-2025` → `windows-2025`, `*-macos-*` →
  `macos-*`, `windows-latest-32-core` → `windows-latest`, `…-arm-core` →
  `windows-11-arm`), and `useblacksmith/checkout`, `setup-docker-builder` and
  `build-push-action` are swapped for `actions/checkout`,
  `docker/setup-buildx-action` and `docker/build-push-action`. Re-apply after
  every re-import:
  `grep -rlE 'blacksmith|windows-latest-32' .github`.

Dropped at this re-import because upstream fixed or superseded them: the
first import's CI repairs, its dependency pins (upstream's lockfiles are newer;
re-run OSV), and the slash-worker cold-start test budget.

## If you re-import a newer upstream

Do a real three-way merge; the patch alone will not apply across a large jump.

1. Clone upstream (blobless is enough) and check out the current base
   (`v0.23.1`) on a branch.
2. Replace that branch's tree with this repo's tree (`git archive origin/main`)
   and commit it: that commit is "LuminaVault on its base".
3. Check out the new upstream tag and `git merge` that branch. Only files both
   sides touched conflict.
4. Take upstream for CI, lockfiles and dependency bumps. For each feature file,
   take upstream and re-apply this repo's diff by hand; upstream moves code
   between modules often (0.19–0.23 split `api_server.py`, `gateway/run.py`
   and `transcription_tools.py`).
5. Watch rename detection: it moved this repo's `docs/` files into
   `website/docs/` once.
6. Compare test failures against pristine upstream on the same machine, not
   against zero: many upstream tests assume Linux CI.
7. Regenerate `luminavault-delta.patch`, update this file, re-run OSV.
