"""FastAPI router exposing gateway portal metadata to authenticated WebUI clients."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.utils.auth import get_verified_user


class GatewayPortalRouter:
    """Builds the gateway bridge portal configuration router."""

    class PortalInfo(BaseModel):
        """Portal integration descriptor returned by ``GET /api/v1/gateway-bridge/portal``."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        enabled: bool
        url: str
        label: str

    @classmethod
    def resolve_portal_info(cls, request: Request) -> GatewayPortalRouter.PortalInfo:
        """Compute the active portal status, target URL, and sidebar label.

        Args:
            request: Incoming FastAPI HTTP request.

        Returns:
            Immutable ``PortalInfo`` describing whether the portal is active.
        """
        cached = getattr(request.app.state, 'gateway_bridge_settings', None)
        settings = cached if isinstance(cached, GatewayBridgeSettings) else GatewayBridgeSettings.load()
        portal_url = settings.portal_url
        parsed = urlsplit(portal_url) if portal_url else None
        is_safe_https = (
            parsed is not None
            and parsed.scheme == 'https'
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
        enabled = bool(settings.enabled and portal_url and is_safe_https)
        return cls.PortalInfo(
            enabled=enabled,
            url=portal_url if enabled else '',
            label=settings.normalized_portal_label(),
        )

    @classmethod
    def build(cls) -> APIRouter:
        """Construct the FastAPI ``APIRouter`` for gateway bridge endpoints.

        Returns:
            Configured ``APIRouter`` ready to mount at ``/api/v1/gateway-bridge``.
        """
        router = APIRouter()

        @router.get(
            '/portal',
            response_model=cls.PortalInfo,
            dependencies=[Depends(get_verified_user)],
        )
        async def get_portal_config(request: Request) -> GatewayPortalRouter.PortalInfo:
            """Return configured portal visibility, HTTPS URL, and navigation label.

            Args:
                request: Current authenticated HTTP request.

            Returns:
                Portal configuration payload for the WebUI sidebar and portal page.
            """
            return cls.resolve_portal_info(request)

        return router
