# Upstream Integration Guide: Gateway Bridge & Portal

This repository extends upstream Open WebUI with an isolated **Gateway Bridge** backend package and **Portal** frontend module so downstream deployments can configure model/media/tool backends and embed an external HTTPS portal without scattering custom logic across upstream files.

## Design Goals

1. **Isolation**: All custom backend logic lives in `backend/open_webui/gateway_bridge/`, and all custom UI lives in `src/lib/components/portal/` and `src/routes/(app)/portal/`.
2. **Zero-impact fallback**: When `GATEWAY_BRIDGE_SETTINGS_FILE` is absent or `"enabled": false`, startup bootstrap is a no-op, media endpoints keep stock upstream behavior, and the portal sidebar item stays hidden.
3. **Rebase clarity**: Every modification inside an upstream file carries a `gateway-bridge` marker comment (`# gateway-bridge` in Python, `<!-- gateway-bridge -->` in Svelte).

## Custom Modules

### Backend (`backend/open_webui/gateway_bridge/`)

- `__init__.py` - Package exports: `GatewayBridgeBootstrap`, `GatewayBridgeSettings`, `GatewayMediaSessionAuth`, `GatewayPortalRouter`.
- `bridge_settings.py` - Strict frozen Pydantic settings model (`GatewayBridgeSettings`, `extra="forbid"`) loading JSON from `GATEWAY_BRIDGE_SETTINGS_FILE` (default `/run/secrets/openwebui-settings.json`).
- `bridge_bootstrap.py` - `GatewayBridgeBootstrap.apply(app)` runs during `lifespan` startup before runtime config initialization and writes OpenAI, RAG embedding/reranker, Audio TTS/STT, Image generation/edit, and native MCP `tool_server.connections` settings into Open WebUI `Config`.
- `media_session_auth.py` - `GatewayMediaSessionAuth` resolves per-user OAuth session bearer tokens for gateway TTS/STT/Image requests, checks target URLs against the configured `gateway_base_url`, disables HTTP redirects on credentialed gateway calls, validates WAV/MP3 speech audio and PNG image responses, and maintains private temporary speech/image helpers when `system_oauth_media` is enabled.
- `portal_router.py` - `GatewayPortalRouter.build()` provides `GET /api/v1/gateway-bridge/portal` returning `{enabled, url, label}` for authenticated verified users.

### Frontend (`src/`)

- `src/lib/components/portal/PortalNavItem.svelte` - Fetches `/api/v1/gateway-bridge/portal`, validates an HTTPS `url` without embedded credentials, and renders collapsed/expanded sidebar navigation items linking to `/portal` with the configured `label`.
- `src/routes/(app)/portal/+page.svelte` - Renders the embedded HTTPS portal `iframe`, relays theme changes via `owui:theme` `postMessage`, synchronizes sub-routes via `portal:route` hash fragments, and exposes an `Open standalone` link.

## Upstream Integration Hooks

Search all upstream hooks in seconds:

```bash
rg -n "gateway-bridge" backend/ src/
```

| Upstream File | Hook Purpose |
| --- | --- |
| `backend/open_webui/main.py` | Imports `GatewayBridgeBootstrap` and `GatewayPortalRouter`; runs `await GatewayBridgeBootstrap.apply(app)` inside `lifespan` before `initialize_runtime_config(app)`; mounts `GatewayPortalRouter.build()` at `/api/v1/gateway-bridge`. |
| `backend/open_webui/utils/oauth.py` | Enforces `ENABLE_OAUTH_ROLE_MANAGEMENT` guards so first/single-user admin auto-promotion is skipped when OAuth role management is enabled and group membership is enforced whenever OAuth role/group lists are configured. |
| `backend/open_webui/routers/audio/__init__.py` | Integrates `GatewayMediaSessionAuth` into OpenAI TTS (`_tts_openai` / `speech`) and STT (`_transcribe_openai`) paths when `system_oauth_media` is active, keeping stock behavior when disabled. |
| `backend/open_webui/routers/images.py` | Integrates `GatewayMediaSessionAuth` into OpenAI image generation (`image_generations`) and edit (`image_edits`) paths when `system_oauth_media` is active, keeping stock behavior when disabled. |
| `src/lib/components/layout/Sidebar.svelte` | Imports `PortalNavItem` and mounts `<PortalNavItem collapsed={true} />` in the collapsed icon rail plus `<PortalNavItem />` in the expanded navigation list. |

## Configuration Reference

Runtime JSON path environment variable:

- `GATEWAY_BRIDGE_SETTINGS_FILE` (default: `/run/secrets/openwebui-settings.json`)

Supported JSON schema fields (strictly validated; unknown keys are rejected):

- `enabled` (`bool`): Master switch for bootstrap and portal enablement.
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

## Rebase Checklist

1. Fetch upstream tag/branch and rebase or merge onto the target upstream commit.
2. Inspect any conflicts in the 5 upstream hook files listed above; preserve the `gateway-bridge` hook call sites while adopting surrounding upstream refactors.
3. Verify `Config` key names in `backend/open_webui/config.py` (`openai.*`, `rag.*`, `audio.*`, `image_generation.*`, `images.edit.*`, `tool_server.connections`) match `bridge_bootstrap.py`.
4. Verify OAuth role checks in `backend/open_webui/utils/oauth.py` still prevent unintended admin promotion or ungrouped access under `ENABLE_OAUTH_ROLE_MANAGEMENT`.
5. Run backend AST/lint checks on `backend/open_webui/gateway_bridge/`, syntax/compilation checks on touched backend modules, and frontend typecheck/build (`npm run check` / `vite build`).
