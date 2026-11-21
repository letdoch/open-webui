# Open WebUI Fork Workplan and Open Backlog

- **Fork repository:** `https://github.com/letdoch/open-webui.git` (public fork, branch `main`; published `origin/main` = `49713d6b8`; upstream base `v0.13.3`)
- **Accountable owner:** Fork maintainers (`backend/open_webui/gateway_bridge/`, custom `src/` routes/components, `gateway-bridge` hooks, `docs/fork/`). Downstream deployment manifests, PostgreSQL/OIDC provisioning, and external landing gates belong to downstream deployment tooling outside this repository.
- **Architecture & references:** [docs/fork/ARCHITECTURE.md](ARCHITECTURE.md) | [docs/UPSTREAM_INTEGRATION.md](../UPSTREAM_INTEGRATION.md) | [README.md](../../README.md) | [.agents/skills/open-webui-development/SKILL.md](../../.agents/skills/open-webui-development/SKILL.md)

*(Note: For upstream Open WebUI's own product roadmap, see the [upstream roadmap](https://docs.openwebui.com/roadmap/) linked in [README.md](../../README.md). This document tracks only fork-owned integration work.)*

## 1. Purpose and Present-vs-Target Boundary

This fork keeps Open WebUI rebaseable on upstream while adding declarative gateway/OIDC configuration (`gateway_bridge/`), an embedded HTTPS `/portal`, and hierarchical `tree_notes/`.

- **Present (`origin/main` `49713d6b8`):** `gateway_bridge/`, `/portal`, and `tree_notes/` are on `origin/main` (`requires-python = ">= 3.11, < 3.13.0a1"`); several `tree_notes/` and `media_session_auth` hardening items remain open.
- **Target:** Zero strict-typing, AST, docstring, and Ruff complexity (`<= 10`) findings across all fork-owned paths; hardened `tree_notes/` search/tools/promote/permissions and atomic multi-replica media caching; and resolved Python runtime compatibility plan.

## 2. Deliverables, Sequencing, and Dependencies

| Phase / Deliverable | Scope & Dependencies | Status |
| --- | --- | --- |
| **Phase 1: Fork Code & Doc Hygiene (`GB-TODO-08`, `09`, `11`)** | Reduce 7 `tree_notes/` methods to Ruff CC `<= 10` (`08`), deduplicate `gateway_bridge` helpers (`09`), and update doc alignment (`11`). | `GB-TODO-11` fork docs updated; `08`, `09` **open**. |
| **Phase 2: `tree_notes/` & Media Runtime Hardening (`GB-TODO-03`..`07`, `10`, `12`)** | Clean `$defs` in `utils/tools.py` (`03`), enforce `notes.enable`/`features.notes` in `get_status` (`04`), push search/pagination to SQL (`05`), close two-phase promote split-brain (`06`), make speech cache atomic/HA-safe (`07`), fix `deque`/SQLite/Svelte state (`10`), and document PG FK authority + schema versioning (`12`). | **Open**. |
| **Phase 3: Python Runtime Evaluation (`GB-TODO-13`)** | Evaluate upstream Open WebUI dependency compatibility against newer Python targets (`13`). | **Open**. |

## 3. Open Indexed TODOs

Source baseline: 2026-11-21 fork review (`origin/main` `49713d6b8`).

| ID | Priority | Target | Summary & Acceptance Gate |
| --- | --- | --- | --- |
| `GB-TODO-03` | **Major** | `backend/open_webui/utils/tools.py`, `gateway_bridge/tree_notes/tools.py` | Recurse `clean_properties` into `$defs` (or flatten tool params) and add `Field(description=...)` on `CreateToolParams`/`UpdatePartToolParams`. Gate: inspect generated OpenAI tool specs and invoke against live gateway. |
| `GB-TODO-04` | **Major** | `gateway_bridge/tree_notes/{router,service}.py` | Evaluate `notes.enable` and `features.notes` in `TreeNoteService.get_status` so unauthorized callers are not routed to `/tree-notes`. Gate: verified non-permitted user receives `enabled=false`. |
| `GB-TODO-05` | **Major** | `gateway_bridge/tree_notes/repository_queries.py` | Push `node_kind`, `archived_at`, tag, text filters, and `LIMIT`/`OFFSET` into SQL; hydrate parts/breadcrumbs only for matched page IDs. Gate: bounded SQL query count on multi-thousand-note dataset. |
| `GB-TODO-06` | **Major** | `gateway_bridge/tree_notes/{service,repository}.py`, `PromoteModal.svelte` | Reconcile two-phase external promotion so concurrent note edits do not discard a created `external_id`, and bind modal `idempotency_key` per intent. Gate: concurrent edit during slow promote endpoint. |
| `GB-TODO-07` | **Major** | `gateway_bridge/media_session_auth.py`, `routers/audio/__init__.py` | Write speech audio/`.mime` atomically (`os.replace`) or move cache to shared storage for N-replica HA, and chmod `gateway-media-private` `0700`. |
| `GB-TODO-08` | Minor | `gateway_bridge/tree_notes/{html_markdown,legacy_importer,parts_reconciler,repository_queries,tools}.py` | Reduce 7 methods from CC 11-12 to `<= 10` (`pyproject.toml` Ruff `max-complexity = 10`) and keep `repository.py` well under 1000 LoC. Gate: `ruff check backend/open_webui/gateway_bridge/`. |
| `GB-TODO-09` | Minor | `gateway_bridge/{media_session_auth,portal_router,bridge_bootstrap,bridge_settings}.py`, `tree_notes/{legacy_importer,repository}.py` | Consolidate settings lookup and HTTPS URL validation on `GatewayBridgeSettings`, reuse `TreeNoteTreeHelpers.insert_parsed_parts` and `load_siblings`. |
| `GB-TODO-10` | Minor | `tree_notes/{repository_parts,repository}.py`, `PartOutlineEditor.svelte`, `PromoteModal.svelte` | Use `collections.deque`, guard SQLite `BEGIN IMMEDIATE` when `autobegin` is active, fix `PromoteModal` reset on open, and debounce `PartOutlineEditor` preview. |
| `GB-TODO-11` | Minor | `docs/UPSTREAM_INTEGRATION.md`, `README.md` | Maintain exact upstream hook-file inventory (`7` code files + `README.md` in `UPSTREAM_INTEGRATION.md`) and keep fork docs free of internal downstream identifiers. |
| `GB-TODO-12` | Minor | `gateway_bridge/bridge_bootstrap.py` | Keep `tree_note_*` out of Alembic, document PostgreSQL as mandatory for FK enforcement, and add explicit schema-version handling for future `tree_note_*` migrations. |
| `GB-TODO-13` | Minor | `pyproject.toml`, `uv.lock` | Upstream `v0.13.3` constrains `requires-python = ">= 3.11, < 3.13.0a1"`; evaluate downstream Python 3.15 compatibility across upstream dependencies (`numpy`, `pyarrow`, `onnxruntime`, etc.) and keep `gateway_bridge/` Python 3.11+ syntax clean. |

## 4. Acceptance and Evidence Gates

1. **Public hygiene & isolation gate:** Zero policy manifest files and zero secrets, private hostnames, or internal downstream identifiers anywhere in the fork; every upstream file edit carries a `gateway-bridge` marker comment; `git diff <upstream-base>..HEAD --stat` stays confined to isolated fork paths + tagged hook files.
2. **Backend lint & compliance gate:** `ruff check backend/open_webui/gateway_bridge/` passes (`max-complexity = 10`, single quotes, Google docstrings); `python -m compileall -q backend/open_webui/gateway_bridge` passes; external downstream static-contract scan passes on fork paths.
3. **Frontend & artifact gate:** `npm run check` / `vite build` passes; `bun scripts/encryption/artifact.mjs` passes; rebase checklist in [docs/UPSTREAM_INTEGRATION.md](../UPSTREAM_INTEGRATION.md#rebase-checklist) verified.
4. **Integrated runtime gate:** Real PostgreSQL + OIDC startup, `system_oauth` media/chat/MCP verification, and `tree_notes` CRUD/CAS/delete-user cascade.
