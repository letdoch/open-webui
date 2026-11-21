# Open WebUI Fork Architecture (`gateway_bridge`)

- **Fork repository:** `https://github.com/letdoch/open-webui.git` (public fork, branch `main`; published `origin/main` = `49713d6b8613f1dbc4abc534f2ce92539bb9d80a`; upstream base `v0.13.3` `b0e6098e940942281d9c83f3529ea117287a3a22`)
- **Delta scope:** Published `origin/main` (`49713d6b8`) adds 37 fork code/UI files (`26` in `gateway_bridge/`, `11` custom UI files) plus `AGENTS.md` and `docs/UPSTREAM_INTEGRATION.md`, and modifies 7 upstream code files; this documentation set adds `docs/fork/{ARCHITECTURE,WORKPLAN}.md` and `.agents/skills/open-webui-development/SKILL.md` and tags `README.md` (`8` modified upstream files total).
- **Fork docs:** [README.md](../../README.md) | [AGENTS.md](../../AGENTS.md) | [WORKPLAN.md](WORKPLAN.md) | [UPSTREAM_INTEGRATION.md](../UPSTREAM_INTEGRATION.md)
- **Development skill:** [.agents/skills/open-webui-development/SKILL.md](../../.agents/skills/open-webui-development/SKILL.md)

## 1. Upstream vs. Fork Boundary and Public Hygiene

This repository is a **public**, upstream-rebaseable fork of Open WebUI `v0.13.3`.

- **Minimal fork isolation:** All custom backend logic lives under `backend/open_webui/gateway_bridge/` (26 files), and all custom frontend modules live under `src/lib/components/portal/`, `src/routes/(app)/portal/`, `src/lib/components/notes_tree/`, and `src/routes/(app)/tree-notes/` (11 files). Upstream files carry tagged `gateway-bridge` marker comments (`# gateway-bridge`, `// gateway-bridge`, or `<!-- gateway-bridge -->`) across 7 modified upstream code files plus `README.md` (8 total).
- **Upstream preservation:** Unchanged upstream modules, Alembic `migrations/versions/`, upstream `README.md` branding/license, and upstream `roadmap` links remain unmodified to minimize rebase conflicts; do not rewrite upstream modules wholesale or alter upstream `note` tables.
- **Public-repository restriction:** This repository contains no secrets, no private deployment hostnames, no policy manifest files, and no internal downstream project/component/audit identifiers inside fork code, UI, docs, or skills.
- **Python runtime constraint (`pyproject.toml`):** Upstream `v0.13.3` constrains `requires-python = ">= 3.11, < 3.13.0a1"`, and `gateway_bridge/` keeps Python 3.11+ compatible syntax (`from __future__ import annotations`). Evaluating upstream dependency compatibility for newer Python runtimes is tracked under `GB-TODO-13` in [WORKPLAN.md](WORKPLAN.md).

## 2. Present-vs-Target Boundary

| Area | Present Baseline (`49713d6b8` `origin/main`) | Target Boundary |
| --- | --- | --- |
| Bridge configuration & startup (`origin/main`) | `GatewayBridgeSettings` (`extra='forbid'`) loads JSON from `GATEWAY_BRIDGE_SETTINGS_FILE`; `GatewayBridgeBootstrap.apply(app)` configures `Config` in-memory (`ENABLE_PERSISTENT_CONFIG=false`) and creates `tree_note_*` tables under `pg_advisory_xact_lock`. | Add explicit schema-version handling for future `tree_note_*` migrations (`GB-TODO-12`) while keeping `tree_note_*` outside Alembic. |
| Per-user OAuth & media (`origin/main`) | `GatewayMediaSessionAuth` forwards per-user `system_oauth` tokens for OpenAI chat, MCP, TTS/STT, and images; disables HTTP redirects and validates WAV/MP3/PNG headers. `private_speech_cache` writes local disk files non-atomically. | Atomic `.mime` + audio writes or shared cache for multi-replica HA (`GB-TODO-07`); keep browser-direct, Azure, and static-key legacy paths disabled. |
| Tree Notes & tools (`tree_notes/`, `origin/main`) | 5-table SQLAlchemy 2.0 3NF graph (`tree_note_{node,daily_entry,part,tag,external_ref}`), optimistic `basis_sha256` CAS, REST endpoints, and 4 built-in chat tools. Search loads owner forest in Python; `get_status` skips `notes.enable`/`features.notes`; `$defs` uncleaned in `utils/tools.py`. | Push search filters/pagination to SQL (`GB-TODO-05`), enforce runtime permission checks in `get_status` (`GB-TODO-04`), clean `$defs` schemas (`GB-TODO-03`), harden two-phase promote recovery (`GB-TODO-06`), and reduce Ruff McCabe `<= 10` (`GB-TODO-08`). |

## 3. Authoritative Owners in the Fork Delta

Authoritative schemas live in `backend/open_webui/gateway_bridge/bridge_settings.py`, `portal_router.py`, `tree_notes/*_model.py`, `tree_notes/contracts*.py`, and `tree_notes/tools.py`:

- **Runtime bridge configuration:** `GatewayBridgeSettings` (`ConfigDict(strict=True, frozen=True, extra='forbid')`).
- **Bootstrap projection:** `GatewayBridgeBootstrap.apply(app)` owns startup injection into Open WebUI `Config` (`openai.*`, `rag.*`, `audio.*`, `image_generation.*`, `images.edit.*`, `tool_server.connections`) and idempotent `tree_note_*` DDL creation.
- **Media OAuth boundary:** `GatewayMediaSessionAuth` owns base-URL origin pinning, redirect refusal, per-user `OAuthManager.get_oauth_token` lookup, and media byte-signature verification.
- **Tree Notes persistence:** `TreeNoteNode`, `TreeNoteDailyEntry` (subtype FK `node_kind = 'daily'`), `TreeNotePart`, `TreeNoteTag`, and `TreeNoteExternalRef` own owner-scoped relational integrity (`user.id` `ON DELETE RESTRICT`, cleaned transactionally in `UsersTable.delete_user_by_id`). PostgreSQL is authoritative for FK enforcement (upstream SQLite omits `PRAGMA foreign_keys = ON`).

## 4. Interface Inventory

Paired 1:1 with the downstream platform interface catalog (`GB-IF-01`..`GB-IF-06`, `GB-IF-08`). Authoritative schemas live in the referenced Python/Svelte files rather than being copied here.

| Interface ID | Surface / Endpoint | Direction | Authoritative Schema / Owner | Auth, Lifetime & Failure Semantics | Status & Open Backlog |
| --- | --- | --- | --- | --- | --- |
| `GB-IF-01` | Runtime settings JSON (`GATEWAY_BRIDGE_SETTINGS_FILE`, default `/run/secrets/openwebui-settings.json`) | Deployer -> Open WebUI startup | `GatewayBridgeSettings` (`bridge_settings.py`) | Strict frozen Pydantic (`extra='forbid'`); invalid file or bounds refuse startup; absent/`enabled: false` leaves paths stock. | Accepted & active (`origin/main`; cross-repo schema parity check manual). |
| `GB-IF-02` | Startup `Config` projection & `tree_note_*` DDL (`GatewayBridgeBootstrap.apply`) | Open WebUI lifespan -> `Config` + PostgreSQL + configured gateway/MCP | `bridge_bootstrap.py`, `main.py` hook | Runs before `initialize_runtime_config(app)`; uses `pg_advisory_xact_lock` for `create_all`; configures `openai.*`, `rag.*`, `audio.*`, `image_generation.*`, `tool_server.connections`. | Accepted & active (`GB-TODO-12` schema-version handling & PG FK note). |
| `GB-IF-03` | Per-user `system_oauth` forwarding & OAuth role guard (`GatewayMediaSessionAuth`, `utils/oauth.py`, `routers/{audio,images}`) | Open WebUI -> OAuth gateway | `media_session_auth.py`, `utils/oauth.py` | Encrypted `oauth_session` token per verified user; redirects disabled (`allow_redirects=False`); WAV/MP3/PNG signature checks; skips admin auto-promotion under `ENABLE_OAUTH_ROLE_MANAGEMENT`. | Accepted & active (`GB-TODO-07` non-atomic local TTS `.mime` cache; upstream static-key paths kept disabled). |
| `GB-IF-04` | Embedded Portal API & iframe bridge (`GET /api/v1/gateway-bridge/portal`, `/portal`, `Sidebar.svelte`) | WebUI browser <-> Open WebUI backend <-> embedded portal origin | `GatewayPortalRouter.PortalInfo` (`portal_router.py`), `PortalNavItem.svelte`, `portal/+page.svelte` | `Depends(get_verified_user)`; strict HTTPS origin without credentials; `postMessage` (`owui:theme`, `portal:route`) gated by exact `event.origin` and `event.source`. | Accepted & active (`origin/main`). |
| `GB-IF-05` | Tree Notes REST API & 5-table graph (`/api/v1/gateway-bridge/tree-notes/*`, `UsersTable.delete_user_by_id`) | WebUI `/tree-notes` <-> `TreeNoteRouter` <-> PostgreSQL | `tree_notes/router.py`, `contracts*.py`, `*_model.py` | `get_verified_user` + `notes.enable` + `features.notes`; owner-only `404`-before-`403`/`409`; `basis_sha256` optimistic CAS; transactional user-delete hook. | Implemented & active (`GB-TODO-04`, `05`, `08`, `09`, `10`, `11` open). |
| `GB-IF-06` | Built-in Tree Notes chat tools (`TreeNoteTools`) & promotion client (`TreeNotePromoteClient`) | Assistant/OpenAI tool loop <-> `TreeNoteTools`; Open WebUI -> `tree_notes_promote_url` | `tree_notes/tools.py`, `promote_client.py`, `utils/tools.py` | Owner-scoped via `ToolUserProbe`; `promote_client` requires HTTPS, disables redirects, forwards caller `system_oauth` bearer (`503` if URL unset). | Implemented (`tools`) / uncommissioned target (`promote_url`) (`GB-TODO-03`, `06`). |
| `GB-IF-08` | Retired runtime overlay & external adapter boundary | N/A | `docs/UPSTREAM_INTEGRATION.md` | Superseded runtime monkey-patching of stock images; optional external OpenAI adapter remains outside this fork repository. | Retired (`overlay`) / external (`adapter`). |

## 5. Verification Boundary

- **Verified at `origin/main` `49713d6b8`:** Static inventory and delta review of the 26 `gateway_bridge/` backend modules, 11 custom UI modules, and 7 modified upstream code files.
- **Not proved:** SQLite `BEGIN IMMEDIATE` under SQLAlchemy autobegin (`GB-TODO-10`), downstream model compatibility with `$defs` `anyOf` tool schemas (`GB-TODO-03`), and Python 3.15 runtime dependency compatibility (`GB-TODO-13`).
