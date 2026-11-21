---
name: open-webui-development
description: Work safely on the public Open WebUI fork (gateway_bridge, portal, tree_notes) while preserving upstream rebaseability, zero private leakage, Ruff/Pydantic standards, and interface parity.
---

# Developing the Open WebUI Fork

## Local decisions

1. **Strict public-fork hygiene:** This repository is **public**. Never commit secrets, internal hostnames, private repository names, policy manifests, or internal downstream project/component/audit identifiers in fork code, UI, docs, or skills (`docs/fork/ARCHITECTURE.md` section 1).
2. **Upstream isolation:** Put all new backend logic in `backend/open_webui/gateway_bridge/` and all new UI in `src/lib/components/{portal,notes_tree}/` and `src/routes/(app)/{portal,tree-notes}/`. Limit upstream file edits to minimal tagged `# gateway-bridge` / `// gateway-bridge` / `<!-- gateway-bridge -->` hooks; never rewrite upstream `models/`, `routers/`, or `migrations/versions/` wholesale (`docs/UPSTREAM_INTEGRATION.md`).
3. **Preserve upstream identity & roadmap:** Never remove Open WebUI branding/license (`LICENSE`) or repurpose upstream `README.md` `roadmap` links. Keep fork plans strictly in `docs/fork/WORKPLAN.md`.
4. **Declarative bridge config & zero-impact fallback:** All fork features toggle from `GATEWAY_BRIDGE_SETTINGS_FILE` (`GatewayBridgeSettings`, `extra='forbid'`); disabled settings must leave stock Open WebUI behavior untouched.
5. **No unit tests in `gateway_bridge/`:** Verify changes via real PostgreSQL, OIDC, and frontend integration runs (`docs/fork/WORKPLAN.md` section 4).

## Commands

- Backend syntax & Ruff lint (`pyproject.toml` sets `max-complexity = 10`, single quotes, Google docstrings):
  `uv run ruff check backend/open_webui/gateway_bridge/`
- Frontend typecheck, build & encryption artifact check:
  `npm run check`, `npm run build` (`vite build`), `bun scripts/encryption/artifact.mjs`
- Hook audit across upstream files:
  `rg -n "gateway-bridge" backend/ src/ README.md`
- Run the downstream static-contract compliance scan externally against fork paths without adding policy files to this repository.

## Binding laws

- **Python version boundary:** Upstream `v0.13.3` declares `requires-python = ">= 3.11, < 3.13.0a1"` in `pyproject.toml`; keep `gateway_bridge/` syntax Python 3.11+ compatible (`from __future__ import annotations`) while tracking newer-Python dependency evaluation under `GB-TODO-13`.
- **Typing & AST rules in `gateway_bridge/`:** Strict Pydantic v2 (`extra='forbid'`, closed `extra='ignore'` probes at upstream dict boundaries), zero `Any`, `object`, `weakref`, bare containers, `or {}`, or `.get()` fallback chains; one top-level class per module; `<= 1000` LoC/file; Ruff McCabe `<= 10`; complete Google docstrings.
- **Database FKs:** `tree_note_*` tables use SQLAlchemy 2.0 `Base` (`create_all` under `pg_advisory_xact_lock`, outside Alembic); PostgreSQL is required for FK `RESTRICT` enforcement (`UsersTable.delete_user_by_id` deletes user notes transactionally).
- **Interface parity:** Use `bridge_settings.py`, `portal_router.py`, and `tree_notes/*` as authoritative schemas; keep `GB-IF-*` rows aligned between `docs/fork/ARCHITECTURE.md` and the downstream interface catalog.

## Runtime check

Syntax and Ruff complexity check without starting containers:

```bash
python -m compileall -q backend/open_webui/gateway_bridge
ruff check backend/open_webui/gateway_bridge/
```

## Realistic proof and rollout

- **Proof:** Exercise `GatewayBridgeBootstrap.apply(app)` against PostgreSQL, OIDC `system_oauth` chat/media/MCP forwarding, and `tree_notes` CRUD/CAS/delete-user cascade (`docs/fork/WORKPLAN.md` section 4).
- **Rollout:** Fork images are built and commissioned by the downstream deployment pipeline (`GATEWAY_BRIDGE_SETTINGS_FILE` + `ENABLE_PERSISTENT_CONFIG=false`); never embed deployment secrets or hostnames into this repository.

## Update routine

1. Work in an owned sibling linked worktree outside the live checkout.
2. Run the Rebase Checklist in `docs/UPSTREAM_INTEGRATION.md` on every upstream merge/rebase.
3. Update `docs/fork/WORKPLAN.md` (`GB-TODO-*`), `docs/fork/ARCHITECTURE.md` (`GB-IF-*`), `docs/UPSTREAM_INTEGRATION.md`, and coordinate with the downstream interface catalog.
4. Verify zero internal-identifier leakage, clean Ruff/compileall/frontend checks, and passing external compliance scan before handing back.
