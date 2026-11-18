# Upstream Integration Guide: Gateway Bridge, Portal & Tree Notes

This repository extends upstream Open WebUI with an isolated **Gateway Bridge** backend package (including **Tree Notes** workspace persistence and tools) plus **Portal** and **Tree Notes** frontend modules so downstream deployments can configure model/media/tool backends, embed an external HTTPS portal, and provide hierarchical/daily notes without scattering custom logic across upstream files. See [fork/ARCHITECTURE.md](fork/ARCHITECTURE.md) and [fork/WORKPLAN.md](fork/WORKPLAN.md) for the fork architecture, interface table, and open backlog.

## Design Goals

1. **Isolation**: All custom backend logic lives in `backend/open_webui/gateway_bridge/`, and all custom UI lives in `src/lib/components/portal/`, `src/routes/(app)/portal/`, `src/lib/components/notes_tree/`, and `src/routes/(app)/tree-notes/`.
2. **Zero-impact fallback**: When `GATEWAY_BRIDGE_SETTINGS_FILE` is absent or `"enabled": false`, startup bootstrap is a no-op, media endpoints keep stock upstream behavior, the portal sidebar item stays hidden, and sidebar `Notes` routing falls back to `/notes`. Setting `"tree_notes_enabled": false` while `"enabled": true` also keeps legacy `/notes` active.
3. **Rebase clarity**: Every modification inside an upstream file carries a `gateway-bridge` marker comment (`# gateway-bridge` in Python, `// gateway-bridge` or `<!-- gateway-bridge -->` in Svelte).

## Custom Modules

### Backend (`backend/open_webui/gateway_bridge/`)

- `__init__.py` - Package exports: `GatewayBridgeBootstrap`, `GatewayBridgeSettings`, `GatewayMediaSessionAuth`, `GatewayPortalRouter`, `TreeNoteRouter`, `TreeNoteTools`.
- `bridge_settings.py` - Strict frozen Pydantic settings model (`GatewayBridgeSettings`, `extra="forbid"`) loading JSON from `GATEWAY_BRIDGE_SETTINGS_FILE` (default `/run/secrets/openwebui-settings.json`), including HTTPS validation for `portal_url` and `tree_notes_promote_url`.
- `bridge_bootstrap.py` - `GatewayBridgeBootstrap.apply(app)` runs during `lifespan` startup before runtime config initialization, configures OpenAI, RAG embedding/reranker, Audio TTS/STT, Image generation/edit, and native MCP `tool_server.connections` settings in Open WebUI `Config`, and initializes the `tree_note_*` database tables idempotently in topological FK order when enabled.
- `media_session_auth.py` - `GatewayMediaSessionAuth` resolves per-user OAuth session bearer tokens for gateway TTS/STT/Image requests, checks target URLs against the configured `gateway_base_url`, disables HTTP redirects on credentialed gateway calls, validates WAV/MP3 speech audio and PNG image responses, and maintains private temporary speech/image helpers when `system_oauth_media` is enabled.
- `portal_router.py` - `GatewayPortalRouter.build()` provides `GET /api/v1/gateway-bridge/portal` returning `{enabled, url, label}` for authenticated verified users.
- `task_ha_settings.py` - `TaskHaSettings`, the strict frozen settings block `task_ha` of `GatewayBridgeSettings`.
- `task_ha/` - Task drain (`drain.py`: states, `SIGTERM` and drain file, admission of external requests, bounded wait started at the signal, cancel marker), instance liveness (`liveness.py`: instance id, heartbeat key, dead-owner check, bounded periodic runner), reaper of dead instances' chats (`reaper.py`), storage protocols and orphan target (`local_tasks.py`, `orphan_store.py`, `task_store.py`, `orphan_target.py`), process runtime assembly (`process_runtime.py`), the adapter to Open WebUI's task, chat and event code (`store.py`) and the `TaskHa` facade used by the hooks (`runtime.py`). Design: [TASK_HA.md](TASK_HA.md).

#### Tree Notes Package (`backend/open_webui/gateway_bridge/tree_notes/`)

- `__init__.py` - Package exports for all Tree Notes models, contracts, codec, repository, service, promote client, router, and built-in tools.
- `digest.py` - `TreeNoteDigest`: deterministic Merkle `basis_sha256` / `part_basis_sha256` canonical JSON hashing (`Unicode NFC`, `\r\n` -> `\n`), pure-Python RFC 9562 monotonic UUIDv7 generation, monotonic nanosecond clock, dotted `part_path` builder, and schema-qualified FK reference helper (`DATABASE_SCHEMA`).
- `node_model.py` - `TreeNoteNode` SQLAlchemy 2.0 ORM model (`tree_note_node`) with single-owner parent hierarchy, composite `(id, owner_user_id, node_kind)` uniqueness, and `pinned` index.
- `daily_entry_model.py` - `TreeNoteDailyEntry` SQLAlchemy 2.0 ORM model (`tree_note_daily_entry`) with composite subtype FK pinning daily entries strictly to `node_kind = 'daily'` and unique `(owner_user_id, note_date)`.
- `part_model.py` - `TreeNotePart` SQLAlchemy 2.0 ORM model (`tree_note_part`) storing addressable Markdown sections/blocks (`list_style`, `heading_level`, `checked`, sibling uniqueness on `sort_order`).
- `tag_model.py` - `TreeNoteTag` SQLAlchemy 2.0 ORM model (`tree_note_tag`) storing normalized lowercase per-node tags.
- `external_ref_model.py` - `TreeNoteExternalRef` SQLAlchemy 2.0 ORM model (`tree_note_external_ref`) with per-owner partial unique indexes for `imported_note` idempotency and upstream promotion references.
- `contracts.py` - Core strict Pydantic v2 DTOs (`TreeNoteContracts`: workspace responses, tree/calendar/search models, create/update/move/reorder/promote requests, CAS `BasisConflictResponse`).
- `contracts_parts.py` - Part-level, tag, Markdown sync, and legacy import strict Pydantic v2 DTOs (`TreeNotePartContracts`) plus tool parameter/response schemas.
- `html_markdown.py` - `TreeNoteHtmlConverter`: `BeautifulSoup` fallback HTML-to-Markdown converter for legacy rich-text notes lacking plain Markdown.
- `parts_codec.py` - `TreeNotePartsCodec`: pure-Python lossless Markdown <-> `TreeNotePart` parser and renderer, nesting matrix validator, `/api/v1/files/{id}/content` reference extractor, and UUID-preserving part reconciliation on whole-Markdown saves.
- `legacy_importer.py` - `TreeNoteLegacyImporter`: idempotent, fault-isolated per-user importer from legacy `note` rows using `sanitize_note_data` + `normalize_pages` (with HTML/TipTap fallback).
- `repository_queries.py` - `TreeNoteRepositoryQueries`: owner-scoped read queries, `WITH RECURSIVE` cycle/depth guards, calendar tree builder, part-level snippet search, and `WITH RECURSIVE` descendant enumeration.
- `repository.py` - `TreeNoteRepository`: fresh non-shared async sessions (`get_async_db()`), per-owner write locks (`SELECT ... FOR UPDATE` on PostgreSQL / `BEGIN IMMEDIATE` on SQLite), atomic daily get-or-create via savepoints, CAS digest checks, subtree delete, and `delete_all_for_user(session, user_id)`.
- `repository_parts.py` - `TreeNotePartRepository`: owner-scoped part creation, structural updates, cross-node move, two-phase sibling reorder, and CAS whole-Markdown replacement.
- `promote_client.py` - `TreeNotePromoteClient`: forwards confirmed task/idea promotions to `tree_notes_promote_url` using the caller's `system_oauth` bearer token via `GatewayMediaSessionAuth.session_token`, strict HTTPS validation, and redirects disabled.
- `service.py` - `TreeNoteService`: orchestrates `notes.enable` / `features.notes` checks, file-read ACL verification (`has_access_to_file`) for added file links, `ZoneInfo` calendar resolution, and `404`-before-`403`/`409` error semantics.
- `router.py` - `TreeNoteRouter`: FastAPI router mounted at `/api/v1/gateway-bridge/tree-notes` exposing `/status`, `/tree`, `/calendar`, `/search`, `/daily/today`, `/daily/{note_date}`, `/import-legacy`, and `/nodes/...` CRUD/part/markdown/tag/archive/promote endpoints.
- `tools.py` - `TreeNoteTools`: owner-scoped chat helper functions (`search_tree_notes`, `view_tree_note`, `create_tree_note`, `update_tree_note_part`) invoked via Open WebUI built-in tool registration.

### Frontend (`src/`)

- `src/lib/components/portal/PortalNavItem.svelte` - Fetches `/api/v1/gateway-bridge/portal`, validates an HTTPS `url` without embedded credentials, and renders collapsed/expanded sidebar navigation items linking to `/portal` with the configured `label`.
- `src/routes/(app)/portal/+page.svelte` - Renders the embedded HTTPS portal `iframe`, relays theme changes via `owui:theme` `postMessage`, synchronizes sub-routes via `portal:route` hash fragments, and exposes an `Open standalone` link.
- `src/lib/components/notes_tree/api.ts` - Typed REST client and error parser (`TreeNoteApiError`, `fetchTreeNotesStatus`) for `/api/v1/gateway-bridge/tree-notes/*`.
- `src/lib/components/notes_tree/store.ts` - Svelte store (`openTreeNoteTabs`) tracking open workspace note tabs.
- `src/lib/components/notes_tree/TreeNodeRow.svelte` - Recursive sidebar tree row for `folder`, `note`, and `task_draft` nodes with expand/collapse, sibling reorder, add-child, and archive badge.
- `src/lib/components/notes_tree/CalendarTree.svelte` - Collapsible `Year -> Month -> Day` daily calendar tree with nested day child nodes.
- `src/lib/components/notes_tree/PartOutlineEditor.svelte` - Multi-mode editor (`Markdown`, `Split`, `Outline & Parts`) supporting interactive checklist toggles, per-part indentation/reordering/deletion, `basis_sha256` conflict recovery banner, and sanitized Markdown preview.
- `src/lib/components/notes_tree/PromoteModal.svelte` - Confirmation modal sending `confirmed: true` with a client-generated UUID `idempotency_key` to `POST /nodes/{id}/promote`.
- `src/lib/components/notes_tree/TreeNotesWorkspace.svelte` - Main Trilium-style workspace UI with collapsible Daily Calendar + Workspace Trees, **Today** quick-open button, full-text/tag search, multi-tab header with breadcrumbs/tags/external refs, **Import Legacy Notes** trigger, and direct link to legacy `/notes`.
- `src/routes/(app)/tree-notes/+page.svelte` - Root `/tree-notes` workspace route.
- `src/routes/(app)/tree-notes/[id]/+page.svelte` - Deep-link `/tree-notes/{id}` route synchronized with route parameter changes.

## Upstream Integration Hooks

Search all upstream hooks in seconds:

```bash
rg -n "gateway-bridge" backend/ src/
```

| Upstream File | Hook Purpose |
| --- | --- |
| `backend/open_webui/main.py` | Imports `GatewayBridgeBootstrap`, `GatewayPortalRouter`, and `TreeNoteRouter`; runs `await GatewayBridgeBootstrap.apply(app)` inside `lifespan` before `initialize_runtime_config(app)`; mounts `GatewayPortalRouter.build()` at `/api/v1/gateway-bridge` and `TreeNoteRouter.build()` at `/api/v1/gateway-bridge/tree-notes`. |
| `backend/open_webui/models/users.py` | Calls `await TreeNoteRepository.delete_all_for_user(session, id)` inside `UsersTable.delete_user_by_id` on the caller's existing transaction before deleting `"user"`. |
| `backend/open_webui/utils/tools.py` | Registers `TreeNoteTools` (`search_tree_notes`, `view_tree_note`, `create_tree_note`, `update_tree_note_part`) for regular chats when `GatewayBridgeBootstrap.is_tree_notes_enabled(request)` is active, and skips forcing `'type': 'string'` on `$ref` parameter schemas in `clean_properties`. |
| `backend/open_webui/utils/oauth.py` | Enforces `ENABLE_OAUTH_ROLE_MANAGEMENT` guards so first/single-user admin auto-promotion is skipped when OAuth role management is enabled and group membership is enforced whenever OAuth role/group lists are configured. |
| `backend/open_webui/routers/audio/__init__.py` | Integrates `GatewayMediaSessionAuth` into OpenAI TTS (`_tts_openai` / `speech`) and STT (`_transcribe_openai`) paths when `system_oauth_media` is active, keeping stock behavior when disabled. |
| `backend/open_webui/routers/images.py` | Integrates `GatewayMediaSessionAuth` into OpenAI image generation (`image_generations`) and edit (`image_edits`) paths when `system_oauth_media` is active, keeping stock behavior when disabled. |
| `src/lib/components/layout/Sidebar.svelte` | Imports `PortalNavItem` and `fetchTreeNotesStatus`; mounts `<PortalNavItem />` in collapsed and expanded rails; routes the pinned `Notes` navigation item to `/tree-notes` (and highlights `/tree-notes/*` as active) when Tree Notes is enabled. |
| `README.md` | Adds a `<!-- gateway-bridge -->` Fork Integration link block under `### Documentation` while preserving upstream `docs`/`roadmap`, branding, and `LICENSE`. |
| `backend/open_webui/tasks.py` | `redis_save_task` stores the owning instance id as the task key value (`TaskHa.owner_token()`); the top of `redis_list_tasks` delegates to `TaskHa.list_tasks` when the feature is enabled (owner-aware listing; the stock body is untouched). |
| `backend/open_webui/main.py` (task drain) | `await TaskHa.start(app, OpenWebuiTaskStore)` after the Valkey client exists and `await TaskHa.quiesce()` right after `yield` in `lifespan`; `/ready` answers 503 while draining; `TaskHa.admit(request)` is the first line of `chat_completion` (external HTTP requests only); `error_detail = TaskHa.cancel_error()` before the re-raise in the `except asyncio.CancelledError` of `process_chat`, so the stock `finally` stores and emits the `server_draining` error. |
| `backend/open_webui/utils/automations.py` | `scheduler_worker_loop` claims no timers, automations or calendar alerts while `TaskHa.is_draining()`. |

## Configuration Reference

Runtime JSON path environment variable:

- `GATEWAY_BRIDGE_SETTINGS_FILE` (default: `/run/secrets/openwebui-settings.json`)

Supported JSON schema fields (strictly validated; unknown keys are rejected):

- `enabled` (`bool`): Master switch for bootstrap, portal, and tree notes enablement.
- `gateway_base_url` (`str`): Base URL for OpenAI-compatible chat/embeddings/reranker/TTS/STT/image APIs.
- `gateway_api_key` (`str`): Gateway API key stored into backend configs that do not use per-user OAuth (`rag.*` / `bearer` auth mode).
- `openai_auth_type` (`str`): Auth mode (`system_oauth`, `bearer`, `session`, `none`; default `system_oauth`) written into `openai.api_configs['0']`.
- `mcp_auth_type` (`str`): Auth mode (`system_oauth`, `bearer`, `session`, `none`; default `system_oauth`) written into `tool_server.connections`.
- `system_oauth_media` (`bool`): Enables per-user OAuth session bearer forwarding for gateway audio and image requests.
- `embedding_model` (`str`), `reranking_model` (`str`), `enable_hybrid_search` (`bool`): RAG embedding and external reranker settings.
- `tts_model` (`str`), `tts_voice` (`str`), `tts_response_format` (`str`), `stt_model` (`str`): Audio synthesis and transcription settings.
- `image_model` (`str`), `image_size` (`str`): Image generation and edit settings.
- `mcp_server_url` (`str`), `mcp_server_id` (`str`), `mcp_server_name` (`str`): Optional native MCP server entry appended/updated in `tool_server.connections` with configured `mcp_auth_type`.
- `portal_url` (`str`), `portal_label` (`str`): Embedded HTTPS portal URL and sidebar navigation label.
- `tree_notes_enabled` (`bool`): Enables the `tree_note_*` schema bootstrap, REST router, built-in chat tools, and `/tree-notes` sidebar routing when `enabled` is `true` (default `true`).
- `tree_notes_promote_url` (`str`): Optional HTTPS endpoint URL for forwarding confirmed task/idea promotions with the caller's `system_oauth` bearer token (leave empty to keep promote unavailable with `503`).
- `task_ha` (object): Task drain and instance liveness for multi-instance deployments ([TASK_HA.md](TASK_HA.md)); inert unless `task_ha.enabled` is `true`, independent of the master `enabled`. Fields: `enabled` (`bool`, default `false`), `max_stream_seconds` (`int` 1-86400, default `300`), `cancel_grace_seconds` (`int` 1-300, default `30`), `instance_heartbeat_seconds` (`int` 1-60, default `5`), `instance_ttl_seconds` (`int` 1-600, default `30`; `ttl - heartbeat` must be at least `valkey_failover_window_seconds + 10`), `valkey_failover_window_seconds` (`int` 1-300, default `15`), `drain_file` (`str`, default empty; a path whose existence starts draining), `drain_settle_seconds` (`int` 0-60, default `5`).

## Rebase Checklist

1. Fetch upstream tag/branch and rebase or merge onto the target upstream commit.
2. Inspect any conflicts in the 7 upstream code hook files listed above (`main.py`, `models/users.py`, `utils/tools.py`, `utils/oauth.py`, `routers/audio/__init__.py`, `routers/images.py`, `Sidebar.svelte`) plus `README.md`; preserve the `gateway-bridge` hook call sites while adopting surrounding upstream refactors.
3. Verify `Config` key names in `backend/open_webui/config.py` (`openai.*`, `rag.*`, `audio.*`, `image_generation.*`, `images.edit.*`, `tool_server.connections`) match `bridge_bootstrap.py`.
4. Verify `UsersTable.delete_user_by_id` in `backend/open_webui/models/users.py` still invokes `TreeNoteRepository.delete_all_for_user(session, id)` before deleting `"user"`, and `clean_properties` in `backend/open_webui/utils/tools.py` preserves `$ref` schemas.
5. Verify OAuth role checks in `backend/open_webui/utils/oauth.py` still prevent unintended admin promotion or ungrouped access under `ENABLE_OAUTH_ROLE_MANAGEMENT`.
6. Run backend AST/lint checks on `backend/open_webui/gateway_bridge/`, syntax/compilation checks on touched backend modules, encryption artifact check (`bun scripts/encryption/artifact.mjs`), and frontend typecheck/build (`npm run check` / `vite build`).
