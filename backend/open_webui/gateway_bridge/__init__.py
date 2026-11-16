"""Clean, upstream-isolated gateway bridge integration for Open WebUI."""

from __future__ import annotations

from open_webui.gateway_bridge.bridge_bootstrap import GatewayBridgeBootstrap
from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.gateway_bridge.media_session_auth import GatewayMediaSessionAuth
from open_webui.gateway_bridge.portal_router import GatewayPortalRouter
from open_webui.gateway_bridge.tree_notes import TreeNoteRouter, TreeNoteTools

__all__ = (
    'GatewayBridgeBootstrap',
    'GatewayBridgeSettings',
    'GatewayMediaSessionAuth',
    'GatewayPortalRouter',
    'TreeNoteRouter',
    'TreeNoteTools',
)
