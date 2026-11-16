"""Strict system_oauth HTTP promotion client for tree notes."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL, AIOHTTP_CLIENT_TIMEOUT
from open_webui.gateway_bridge.media_session_auth import GatewayMediaSessionAuth
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.repository import TreeNoteRepository
from open_webui.models.users import UserModel
from open_webui.utils.session_pool import get_session

log = logging.getLogger(__name__)


class TreeNotePromoteClient:
    """Forwards confirmed node promotions strictly using caller OAuth session credentials."""

    class _OutboundPromotePayload(BaseModel):
        """Typed JSON payload posted to the configured HTTPS promotion endpoint."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node_id: str
        target_kind: str
        title: str
        node_kind: str
        markdown: str
        tags: list[str]
        basis_sha256: str
        idempotency_key: str
        project_id: str | None = None
        summary: str | None = None

    class _DownstreamPromoteResponse(BaseModel):
        """Closed Pydantic validation envelope for downstream promotion responses."""

        model_config = ConfigDict(extra='ignore')

        id: str = ''
        status: str = 'created'
        url: str | None = None

    @classmethod
    async def promote(
        cls,
        request: Request,
        user: UserModel,
        snapshot: TreeNoteRepository.PromoteSnapshot,
        body: TreeNotePartContracts.PromoteRequest,
    ) -> tuple[str, str | None, str]:
        """Execute outbound HTTPS promotion using the authenticated caller's OAuth bearer.

        Args:
            request: Active FastAPI HTTP request.
            user: Verified caller user model.
            snapshot: Locked pre-promotion node snapshot.
            body: Confirmed promotion request with ``idempotency_key``.

        Returns:
            Tuple of ``(verified_external_id, optional_https_target_url, status)``.

        Raises:
            HTTPException: Promotion URL unconfigured (503), OAuth unavailable (401/503),
                or downstream error/invalid payload (502).
        """
        settings = GatewayMediaSessionAuth.resolve_settings(request)
        target_url = settings.normalized_tree_notes_promote_url()
        if not target_url:
            raise HTTPException(status_code=503, detail='Tree notes promotion endpoint is not configured.')
        cls._verify_https_target(target_url)
        token = await GatewayMediaSessionAuth.session_token(request, user)

        outbound = cls._OutboundPromotePayload(
            node_id=snapshot.node_id,
            target_kind=body.target_kind,
            title=snapshot.title,
            node_kind=snapshot.node_kind,
            markdown=snapshot.rendered_markdown,
            tags=snapshot.tags,
            basis_sha256=snapshot.basis_sha256,
            idempotency_key=body.idempotency_key,
            project_id=body.project_id,
            summary=body.summary,
        )
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Idempotency-Key': body.idempotency_key,
        }
        raw_text = await cls._post_no_redirect(target_url, outbound.model_dump_json(), headers)
        return cls._parse_downstream_response(raw_text, target_url)

    @staticmethod
    def _verify_https_target(target_url: str) -> None:
        """Fail closed unless promotion URL is strict HTTPS with host and no userinfo.

        Args:
            target_url: Configured promotion webhook URL.

        Raises:
            HTTPException: URL is missing strict HTTPS, host, or embeds userinfo.
        """
        parts = urlsplit(target_url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise HTTPException(status_code=503, detail='Tree notes promotion URL must use strict HTTPS.')

    @staticmethod
    async def _post_no_redirect(url: str, json_body: str, headers: dict[str, str]) -> str:
        """Send an outbound POST request with redirects strictly disabled.

        Args:
            url: Verified HTTPS target URL.
            json_body: Serialized JSON request payload.
            headers: Outbound request headers including caller Authorization bearer.

        Returns:
            Response body text on 2xx success.

        Raises:
            HTTPException: Upstream returned non-2xx/redirect or transport failed.
        """
        session = await get_session()
        timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        try:
            async with session.post(
                url,
                data=json_body.encode('utf-8'),
                headers=headers,
                allow_redirects=False,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=timeout,
            ) as response:
                body_text = await response.text()
                if response.status >= 400 or 300 <= response.status < 400:
                    log.warning('Tree notes promotion rejected with HTTP %s', response.status)
                    raise HTTPException(
                        status_code=502,
                        detail=f'Promotion target returned HTTP {response.status}.',
                    )
                return body_text
        except HTTPException:
            raise
        except aiohttp.ClientError as exc:
            log.warning('Tree notes promotion transport failed: %s', type(exc).__name__)
            raise HTTPException(status_code=502, detail='Failed to reach promotion endpoint.') from None

    @classmethod
    def _parse_downstream_response(cls, raw_text: str, promote_url: str) -> tuple[str, str | None, str]:
        """Validate downstream promotion JSON and sanitize any returned link URL.

        Args:
            raw_text: Raw JSON response text returned by downstream endpoint.
            promote_url: Origin promotion target URL used for same-origin link sanitization.

        Returns:
            Tuple of ``(external_id, sanitized_url, status_text)``.

        Raises:
            HTTPException: Downstream JSON is invalid or omits target ID.
        """
        try:
            parsed = cls._DownstreamPromoteResponse.model_validate_json(raw_text)
        except ValidationError:
            raise HTTPException(status_code=502, detail='Promotion endpoint returned invalid JSON.') from None
        external_id = parsed.id.strip()
        if not external_id:
            raise HTTPException(status_code=502, detail='Promotion endpoint returned an empty target ID.')
        target_url: str | None = None
        if parsed.url is not None and parsed.url.strip():
            candidate = parsed.url.strip()
            parts = urlsplit(candidate)
            base_parts = urlsplit(promote_url)
            if (
                parts.scheme == 'https'
                and parts.netloc == base_parts.netloc
                and not parts.username
                and not parts.password
            ):
                target_url = candidate
        status_text = parsed.status.strip() or 'created'
        return external_id, target_url, status_text
