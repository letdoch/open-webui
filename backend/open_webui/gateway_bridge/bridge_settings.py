"""Typed file-backed configuration loader for the Open WebUI gateway bridge."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

SETTINGS_FILE_ENV = 'GATEWAY_BRIDGE_SETTINGS_FILE'
DEFAULT_SETTINGS_FILE = '/run/secrets/openwebui-settings.json'


class GatewayBridgeSettings(BaseModel):
    """Immutable, strictly validated runtime settings for gateway bridge integration."""

    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

    enabled: bool = False
    gateway_base_url: str = ''
    gateway_api_key: str = ''
    openai_auth_type: str = 'system_oauth'
    mcp_auth_type: str = 'system_oauth'
    system_oauth_media: bool = False
    embedding_model: str = ''
    reranking_model: str = ''
    enable_hybrid_search: bool = False
    tts_model: str = ''
    tts_voice: str = ''
    tts_response_format: str = ''
    stt_model: str = ''
    image_model: str = ''
    image_size: str = ''
    mcp_server_url: str = ''
    mcp_server_id: str = 'gateway-mcp'
    mcp_server_name: str = 'Gateway MCP'
    portal_url: str = ''
    portal_label: str = 'Portal'

    @field_validator('gateway_base_url', 'mcp_server_url', 'portal_url', mode='after')
    @classmethod
    def _validate_url(cls, value: str, info: ValidationInfo) -> str:
        """Validate optional endpoint URLs and enforce HTTPS on portal URLs.

        Args:
            value: Raw configured URL string.
            info: Pydantic field validation context.

        Returns:
            Normalized URL string, or empty string when unset.

        Raises:
            ValueError: The URL uses a disallowed scheme, lacks a host, or embeds credentials.
        """
        stripped = value.strip()
        if not stripped:
            return ''
        field_name = info.field_name or 'url'
        https_only = field_name == 'portal_url'
        cls._check_url_parts(stripped, field_name, https_only)
        return stripped.rstrip('/') if field_name == 'gateway_base_url' else stripped

    @field_validator(
        'gateway_api_key',
        'embedding_model',
        'reranking_model',
        'tts_model',
        'tts_voice',
        'tts_response_format',
        'stt_model',
        'image_model',
        'image_size',
        'mcp_server_id',
        'mcp_server_name',
        'portal_label',
        mode='after',
    )
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """Trim surrounding whitespace from configured string fields.

        Args:
            value: Raw configured string value.

        Returns:
            Trimmed string value.
        """
        return value.strip()

    @field_validator('openai_auth_type', 'mcp_auth_type', mode='after')
    @classmethod
    def _validate_auth_type(cls, value: str, info: ValidationInfo) -> str:
        """Restrict upstream and MCP authentication modes to supported values.

        Args:
            value: Configured auth type identifier.
            info: Pydantic field validation context.

        Returns:
            Normalized auth type literal.

        Raises:
            ValueError: The auth type is outside the supported modes.
        """
        normalized = value.strip()
        if normalized not in ('system_oauth', 'bearer', 'session', 'none'):
            raise ValueError(f'{info.field_name or "auth_type"} must be one of system_oauth, bearer, session, none')
        return normalized

    @staticmethod
    def _check_url_parts(url: str, field_name: str, https_only: bool) -> None:
        """Validate scheme, host, port, and absence of embedded credentials in a URL.

        Args:
            url: Non-empty URL string to inspect.
            field_name: Configuration field name for clear diagnostics.
            https_only: Whether only the HTTPS scheme is accepted.

        Raises:
            ValueError: The URL is malformed or violates transport rules.
        """
        parts = urlsplit(url)
        allowed_schemes = ('https',) if https_only else ('http', 'https')
        if parts.scheme not in allowed_schemes:
            raise ValueError(f'{field_name} must use {"https" if https_only else "http or https"}')
        if not parts.hostname:
            raise ValueError(f'{field_name} must include a hostname')
        if parts.username or parts.password:
            raise ValueError(f'{field_name} must not embed userinfo credentials')
        _ = parts.port

    @classmethod
    def settings_file_path(cls) -> Path | None:
        """Resolve the configured settings JSON file path from the environment.

        Returns:
            Configured settings file path, or ``None`` when explicitly cleared.
        """
        raw = os.environ.get(SETTINGS_FILE_ENV, DEFAULT_SETTINGS_FILE).strip()
        return Path(raw) if raw else None

    @classmethod
    def load(cls, path: Path | None = None) -> GatewayBridgeSettings:
        """Load gateway bridge settings from disk when the file exists.

        Args:
            path: Optional explicit settings file path override.

        Returns:
            Validated settings instance, or disabled defaults if the file is absent.

        Raises:
            ValueError: The resolved settings path exists but is not a regular file.
        """
        target = path if path is not None else cls.settings_file_path()
        if target is None or not target.exists():
            return cls()
        if not target.is_file():
            raise ValueError(f'Gateway bridge settings path is not a regular file: {target}')
        return cls.model_validate_json(target.read_text(encoding='utf-8'))

    def normalized_gateway_base_url(self) -> str:
        """Return the gateway base URL with any trailing slash removed.

        Returns:
            Normalized gateway API base URL, or an empty string when unset.
        """
        return self.gateway_base_url.rstrip('/')

    def normalized_portal_label(self) -> str:
        """Return the display label for the embedded portal navigation entry.

        Returns:
            Non-empty portal label string.
        """
        return self.portal_label or 'Portal'
