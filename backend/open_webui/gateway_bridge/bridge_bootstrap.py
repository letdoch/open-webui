"""Startup configuration bootstrapper for the Open WebUI gateway bridge."""

from __future__ import annotations

import logging
from typing import TypedDict

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, ValidationError

from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.models.config import Config

log = logging.getLogger(__name__)


class GatewayBridgeBootstrap:
    """Applies validated gateway bridge settings to Open WebUI runtime configuration."""

    class _OpenAIProviderConfig(TypedDict):
        """Typed OpenAI provider entry for Open WebUI model discovery."""

        enable: bool
        auth_type: str
        model_ids: list[str]

    class _ToolServerInfoDict(TypedDict):
        """Typed identity metadata for a native tool server connection."""

        id: str
        name: str

    class _ToolServerConfigDict(TypedDict):
        """Typed toggle block for a native tool server connection."""

        enable: bool

    class _ToolServerConnectionDict(TypedDict):
        """Typed MCP tool server connection entry stored in ``tool_server.connections``."""

        type: str
        auth_type: str
        url: str
        path: str
        key: str
        forward_cookies: bool
        headers: dict[str, str] | None
        info: GatewayBridgeBootstrap._ToolServerInfoDict
        config: GatewayBridgeBootstrap._ToolServerConfigDict

    class _ToolServerInfoProbe(BaseModel):
        """Minimal typed info probe for deduplicating existing tool server entries."""

        model_config = ConfigDict(extra='ignore')

        id: str = ''

    class _ToolServerEntryProbe(BaseModel):
        """Minimal typed connection probe for deduplicating existing tool server entries."""

        model_config = ConfigDict(extra='ignore')

        url: str = ''
        info: GatewayBridgeBootstrap._ToolServerInfoProbe | None = None

    @classmethod
    async def apply(cls, app: FastAPI, settings: GatewayBridgeSettings | None = None) -> GatewayBridgeSettings:
        """Load gateway bridge settings and upsert Open WebUI config when enabled.

        Args:
            app: Active FastAPI application instance.
            settings: Optional preloaded settings override.

        Returns:
            The resolved ``GatewayBridgeSettings`` attached to ``app.state``.
        """
        resolved = settings if settings is not None else GatewayBridgeSettings.load()
        app.state.gateway_bridge_settings = resolved
        if not resolved.enabled:
            return resolved

        base_url = resolved.normalized_gateway_base_url()
        if base_url:
            await cls._apply_openai(base_url, resolved)
            await cls._apply_rag(base_url, resolved)
            await cls._apply_audio(base_url, resolved)
            await cls._apply_images(base_url, resolved)
        else:
            await Config.upsert({'rag.enable_hybrid_search': resolved.enable_hybrid_search})

        if resolved.mcp_server_url:
            await cls._apply_mcp(resolved)

        log.info('Gateway bridge configuration applied during startup.')
        return resolved

    @classmethod
    async def _apply_openai(cls, base_url: str, settings: GatewayBridgeSettings) -> None:
        """Configure the OpenAI-compatible chat/model provider to expose all gateway models.

        Args:
            base_url: Normalized gateway OpenAI-compatible base URL.
            settings: Validated gateway bridge settings.
        """
        provider_config: GatewayBridgeBootstrap._OpenAIProviderConfig = {
            'enable': True,
            'auth_type': settings.openai_auth_type,
            'model_ids': [],
        }
        api_key = '' if settings.openai_auth_type == 'system_oauth' else settings.gateway_api_key
        api_configs: dict[str, GatewayBridgeBootstrap._OpenAIProviderConfig] = {'0': provider_config}
        await Config.upsert(
            {
                'openai.enable': True,
                'openai.api_base_urls': [base_url],
                'openai.api_keys': [api_key],
                'openai.api_configs': api_configs,
            }
        )

    @classmethod
    async def _apply_rag(cls, base_url: str, settings: GatewayBridgeSettings) -> None:
        """Configure RAG embedding, hybrid search, and external reranking settings.

        Args:
            base_url: Normalized gateway OpenAI-compatible base URL.
            settings: Active gateway bridge settings.
        """
        updates: dict[str, bool | str] = {'rag.enable_hybrid_search': settings.enable_hybrid_search}
        if settings.embedding_model:
            updates['rag.embedding_engine'] = 'openai'
            updates['rag.openai.api_base_url'] = base_url
            updates['rag.openai.api_key'] = settings.gateway_api_key
            updates['rag.embedding_model'] = settings.embedding_model
        if settings.reranking_model:
            updates['rag.reranking_engine'] = 'external'
            updates['rag.external_reranker_url'] = f'{base_url}/rerank'
            updates['rag.external_reranker_api_key'] = settings.gateway_api_key
            updates['rag.reranking_model'] = settings.reranking_model
        await Config.upsert(updates)

    @classmethod
    async def _apply_audio(cls, base_url: str, settings: GatewayBridgeSettings) -> None:
        """Configure OpenAI-compatible TTS and STT audio endpoints.

        Args:
            base_url: Normalized gateway OpenAI-compatible base URL.
            settings: Active gateway bridge settings.
        """
        updates: dict[str, str | dict[str, str]] = {}
        if settings.tts_model:
            updates['audio.tts.engine'] = 'openai'
            updates['audio.tts.openai.api_base_url'] = base_url
            updates['audio.tts.openai.api_key'] = settings.gateway_api_key
            updates['audio.tts.model'] = settings.tts_model
            updates['audio.tts.split_on'] = 'punctuation'
            if settings.tts_voice:
                updates['audio.tts.voice'] = settings.tts_voice
            if settings.tts_response_format:
                updates['audio.tts.openai.params'] = {'response_format': settings.tts_response_format}
        if settings.stt_model:
            updates['audio.stt.engine'] = 'openai'
            updates['audio.stt.openai.api_base_url'] = base_url
            updates['audio.stt.openai.api_key'] = settings.gateway_api_key
            updates['audio.stt.model'] = settings.stt_model
        if updates:
            await Config.upsert(updates)

    @classmethod
    async def _apply_images(cls, base_url: str, settings: GatewayBridgeSettings) -> None:
        """Configure OpenAI-compatible image generation and editing endpoints.

        Args:
            base_url: Normalized gateway OpenAI-compatible base URL.
            settings: Active gateway bridge settings.
        """
        if not settings.image_model:
            return
        updates: dict[str, bool | str] = {
            'image_generation.enable': True,
            'image_generation.engine': 'openai',
            'image_generation.openai.api_base_url': base_url,
            'image_generation.openai.api_key': settings.gateway_api_key,
            'image_generation.model': settings.image_model,
            'images.edit.enable': True,
            'images.edit.engine': 'openai',
            'images.edit.openai.api_base_url': base_url,
            'images.edit.openai.api_key': settings.gateway_api_key,
            'images.edit.model': settings.image_model,
        }
        if settings.image_size:
            updates['image_generation.size'] = settings.image_size
            updates['images.edit.size'] = settings.image_size
        await Config.upsert(updates)

    @classmethod
    async def _apply_mcp(cls, settings: GatewayBridgeSettings) -> None:
        """Register or refresh the native MCP tool server connection cleanly.

        Args:
            settings: Active gateway bridge settings.
        """
        server_id = settings.mcp_server_id or 'gateway-mcp'
        server_name = settings.mcp_server_name or 'Gateway MCP'
        gateway_entry: GatewayBridgeBootstrap._ToolServerConnectionDict = {
            'type': 'mcp',
            'auth_type': settings.mcp_auth_type,
            'url': settings.mcp_server_url,
            'path': '',
            'key': settings.gateway_api_key if settings.mcp_auth_type == 'bearer' else '',
            'forward_cookies': False,
            'headers': None,
            'info': {'id': server_id, 'name': server_name},
            'config': {'enable': True},
        }
        raw_existing = await Config.get('tool_server.connections', [])
        preserved = []
        if isinstance(raw_existing, list):
            for entry in raw_existing:
                if not cls._matches_gateway_entry(entry, server_id, settings.mcp_server_url):
                    preserved.append(entry)
        preserved.append(gateway_entry)
        await Config.upsert({'tool_server.connections': preserved})

    @classmethod
    def _matches_gateway_entry(
        cls,
        entry: BaseModel | dict[str, str | bool | None | dict[str, str | bool] | list[str]],
        server_id: str,
        server_url: str,
    ) -> bool:
        """Return whether an existing tool server entry represents this gateway MCP server.

        Args:
            entry: Candidate connection record read from ``tool_server.connections``.
            server_id: Configured MCP server identifier.
            server_url: Configured MCP server URL.

        Returns:
            ``True`` when the entry matches the configured gateway MCP identity or URL.
        """
        try:
            probe = cls._ToolServerEntryProbe.model_validate(entry)
        except ValidationError:
            return False
        probe_id = probe.info.id if probe.info is not None else ''
        return probe_id == server_id or probe.url.rstrip('/') == server_url.rstrip('/')
