# AGENTS.md

This is the public Open WebUI fork that cframe hosts as its chat, RAG and workspace companion; it stays rebaseable on
upstream and is coupled to cframe minimally. This checkout is `components/open-webui` of the cframe repository and is a
separate Git repository. Read the cframe repository's `AGENTS.md` first; it governs this checkout too.

## Access path (D-62)

Open WebUI is a browser-facing, hub-internal service and keeps its ingress by the D-62 scope default (browsers and hub-internal
services are outside the host conduit): the browser reaches it through the hub's Caddy ingress, and it reaches the cllm gateway
and cframe `/mcp` on the hub with the signed-in person's own token. It is not a host component and not a way for host code to reach
cllm or cframe: anything that runs on a host goes through that host's cbootloader. This file names no private hosts, secrets or
deployment details, because the repository is public. The rule: cframe `AGENTS.md`, "cbootloader is the only conduit to cframe".
