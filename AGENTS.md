# AGENTS.md

This repository is a public, upstream-rebaseable fork of Open WebUI used as a browser-facing chat, RAG, and workspace companion. All custom integration logic is isolated from upstream modules so upstream releases remain easy to merge.

Read in order for fork changes:
- [docs/fork/ARCHITECTURE.md](docs/fork/ARCHITECTURE.md) -- fork isolation, authoritative owners, and `GB-IF-*` interface inventory.
- [docs/fork/WORKPLAN.md](docs/fork/WORKPLAN.md) -- fork deliverables, dependencies, and open backlog (`GB-TODO-*`).
- [docs/UPSTREAM_INTEGRATION.md](docs/UPSTREAM_INTEGRATION.md) -- module inventory and upstream hook checklist.
- [.agents/skills/open-webui-development/SKILL.md](.agents/skills/open-webui-development/SKILL.md) -- developer skill (public-repository hygiene, Ruff/Pydantic rules, proof and rebase routine).

## Access path and public-repository rules

- Open WebUI is a browser-facing service: browsers reach it through the deployment's HTTPS reverse-proxy ingress, and the backend reaches configured OpenAI-compatible gateways and MCP servers using the signed-in user's own OAuth token (`system_oauth`). It is not a host-side agent or a conduit for host processes.
- Because this fork repository is public, never commit secrets, private hostnames, deployment infrastructure details, policy manifests, or internal downstream project/component/audit identifiers anywhere in code, UI, docs, or skills.
