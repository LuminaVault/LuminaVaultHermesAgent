# What LuminaVault changed in the vendored Hermes Agent import

This repository is **not a git fork** of `NousResearch/hermes-agent`. It is a
squashed vendored import: `0b99ab2` seeds the repo and `0f5b20a`
("chore: import Hermes Agent with LuminaVault ingestion API") drops the whole
upstream tree in as one commit — 6243 files, 2.26M lines. There is no upstream
remote and no common ancestor, which is why CI carries a
`Deny unrelated histories / check-common-ancestor` job.

The consequence is that **LuminaVault's own changes to upstream files are
invisible in git history** — they are buried inside that one import commit.
Anyone re-importing a newer upstream would silently delete them. This document
is the manifest so that cannot happen.

Established 2026-09-12 by diffing the import tree against upstream tarballs.

## Base commit

The import is a clean snapshot of upstream **`e4ea0a0e`**
(2026-07-13T06:44:16Z, "fix(config): preserve string-typed config values").

Verified by extracting `0f5b20a` and diffing the full tree against that commit:
only the entries below differ. Earlier candidates were ruled out —
`2bd721ce` (08:49:35Z, 45s before the import) already contained a kanban trio
the import lacks, so the import predates it.

## LuminaVault modifications — 2 files, 114 lines, purely additive

Zero upstream lines are removed. The patch is saved alongside this file as
[`luminavault-delta.patch`](./luminavault-delta.patch) (7 hunks).

| File | Lines added |
|---|---:|
| `gateway/platforms/api_server.py` | 65 |
| `tests/gateway/test_api_server.py` | 49 |

What it adds — the **multimodal ingestion API**, which upstream has never had
(`_handle_ingestion`, `v1/ingestions` and `multimodal_ingestion` return zero
hits across every upstream version, including main):

- `_handle_ingestion` → `POST /v1/ingestions`, which analyses a remotely hosted
  multimodal source with the normal agent toolset
- capability advertisement: `multimodal_ingestion`, `ingestion_api`,
  `ingestion_remote_source_url`, `ingestion_supported_mime_types`
  (pdf, text/\*, image/\*, audio/\*, video/\*), `ingestion_max_source_bytes`
  (2 GiB)
- the endpoint entry `"ingestion": {"method": "POST", "path": "/v1/ingestions"}`
- run-stream bookkeeping used by the same surface: `_run_stream_subscribers`,
  `_stopping_run_ids`

**This is load-bearing.** `LuminaVaultServer` has a multimodal ingestion
subsystem wired to it — `ingestion.publicBaseUrl`, `ingestionCapabilitiesService`
and a "Durable multimodal ingestion" path in `Sources/App/App+build.swift`.
Losing it breaks that feature.

## Upstream content the import dropped — 6 paths

All six were lost to `.gitignore` rules inherited from upstream, because the
import staged files with `git add`:

| Path | Rule |
|---|---|
| `infographic/` | `.gitignore:152` — `infographic/` |
| `optional-skills/creative/concept-diagrams/examples` | `.gitignore:39` — `examples/` |
| `web/public/fonts` | `.gitignore:79` — `web/public/fonts/` |
| `skills/creative/p5js/scripts/export-frames.js` | `.gitignore:25` — `export*` |
| `skills/creative/p5js/references/export-pipeline.md` | `.gitignore:25` — `export*` |
| `website/src/data` | `.gitignore:29` — `data/` (**fixed**, see below) |

Two of these are over-broad patterns catching real source rather than build
output: `examples/` swallows a skill's documentation, and `export*` swallows a
skill's script — so that p5js export skill is shipped broken.

`data/` was narrowed to `/data/` in PR #10 after the missing
`website/src/data/userStories.json` broke the docs build on every run. The other
five are still dropped.

## If you re-import a newer upstream

1. Import the new upstream tree, using `git add --force` for the six paths above
   (or fix the over-broad rules first).
2. Re-apply `luminavault-delta.patch`.
3. Expect manual reconciliation: against upstream `main` as of 2026-09-12 the
   patch does **not** apply cleanly. The conflicts are context drift in two dict
   literals (the capability map and the endpoint map) where upstream added
   neighbouring keys — not semantic conflicts. The added code itself is
   self-contained.
4. Re-apply the post-import commits, which *are* visible in `git log`.
5. Re-run the OSV scan; upstream pins will differ from ours.

## Why you might want to

Upstream is at `0.21.2`; this tree declares `0.18.2`. OSV flags that version for
**CVE-2026-10221** — injection via `_compress_context`, remotely triggerable,
public exploit — with range `introduced: 0 → last_affected: 0.19.0`. The
vulnerable code is present here (`run_agent.py`, `agent/context_compressor.py`)
and upstream's guard (`_transcript_has_real_user_turn`, with
`_validate_summary_user_provenance`) is not. The companion CVE-2026-10224 does
not apply — `gateway/platforms/feishu.py` does not exist in this tree.

Porting that guard by hand is awkward: upstream restructured compression across
0.19–0.21 and the fix hooks into seams this tree does not have
(`_build_summary_prompt`, `_find_context_summaries`,
`COMPRESSED_SUMMARY_HAS_USER_TURN_KEY`, `_ground_historical_task_snapshot`).
Now that the local delta is known to be 114 additive lines, re-importing is the
cheaper and more durable route.
