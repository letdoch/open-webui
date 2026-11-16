"""Opt-in per-user OAuth session bearer authentication for gateway media routes."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import wave
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.models.users import UserModel
from open_webui.utils.session_pool import get_session

log = logging.getLogger(__name__)

IMAGE_FILE_SUFFIX_PATTERN = re.compile(r'/images/files/[0-9a-f]{32}\.png\Z')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class GatewayMediaSessionAuth:
    """Resolves and enforces OAuth session bearer credentials for TTS, STT, and image routes."""

    class _OAuthTokenPayload(BaseModel):
        """Validated OAuth session token envelope carrying the access token."""

        model_config = ConfigDict(extra='ignore')

        access_token: str = ''

    @classmethod
    def resolve_settings(cls, request: Request) -> GatewayBridgeSettings:
        """Read active gateway bridge settings from app state or disk.

        Args:
            request: Current FastAPI HTTP request.

        Returns:
            Validated ``GatewayBridgeSettings`` for the current process.
        """
        cached = getattr(request.app.state, 'gateway_bridge_settings', None)
        if isinstance(cached, GatewayBridgeSettings):
            return cached
        loaded = GatewayBridgeSettings.load()
        request.app.state.gateway_bridge_settings = loaded
        return loaded

    @classmethod
    def is_enabled(cls, request: Request) -> bool:
        """Return whether OAuth session bearer authentication is enabled for media routes.

        Args:
            request: Current FastAPI HTTP request.

        Returns:
            ``True`` when the gateway bridge and ``system_oauth_media`` are both enabled.
        """
        settings = cls.resolve_settings(request)
        return bool(settings.enabled and settings.system_oauth_media)

    @classmethod
    def allow_redirects(cls, request: Request) -> bool:
        """Disable outbound redirect following when OAuth media bearers are attached.

        Args:
            request: Current FastAPI HTTP request.

        Returns:
            ``False`` in OAuth media mode, otherwise stock ``True``.
        """
        return not cls.is_enabled(request)

    @classmethod
    def authorized_base(cls, request: Request, base_url: str) -> str:
        """Verify that the active media provider base URL matches the configured gateway URL.

        Args:
            request: Current FastAPI HTTP request.
            base_url: Runtime provider base URL configured for the media route.

        Returns:
            Normalized configured gateway base URL.

        Raises:
            HTTPException: The gateway base URL is missing or differs from the active provider URL.
        """
        settings = cls.resolve_settings(request)
        configured = settings.normalized_gateway_base_url()
        candidate = base_url.strip().rstrip('/')
        if not configured or candidate != configured:
            raise HTTPException(
                status_code=503,
                detail='System OAuth media requires the configured gateway API base URL.',
            )
        return configured

    @classmethod
    async def session_token(cls, request: Request, user: UserModel | None) -> str:
        """Resolve the current request's OAuth session bearer token via ``OAuthManager``.

        Args:
            request: Current authenticated browser request.
            user: Principal verified by the endpoint dependency.

        Returns:
            Non-empty OAuth access token for the active browser session.

        Raises:
            HTTPException: The user, cookie, session manager, or token is missing or expired.
        """
        principal = user.id if user is not None else ''
        session_id = request.cookies.get('oauth_session_id', '')
        if not principal or not session_id:
            raise HTTPException(
                status_code=401,
                detail='System OAuth media requires a signed-in OAuth browser session.',
            )
        manager = getattr(request.app.state, 'oauth_manager', None)
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail='System OAuth media session manager is unavailable.',
            )
        try:
            raw_token = await manager.get_oauth_token(principal, session_id)
        except Exception as exc:
            log.warning('Failed to resolve OAuth session token for user %s: %s', principal, type(exc).__name__)
            raise HTTPException(status_code=503, detail='System OAuth media session is unavailable.') from None
        if raw_token is None:
            raise HTTPException(status_code=401, detail='System OAuth media session expired. Sign in again.')
        try:
            parsed = cls._OAuthTokenPayload.model_validate(raw_token)
        except ValidationError:
            raise HTTPException(status_code=401, detail='System OAuth media session expired. Sign in again.') from None
        access_token = parsed.access_token.strip()
        if not access_token:
            raise HTTPException(status_code=401, detail='System OAuth media session expired. Sign in again.')
        return access_token

    @classmethod
    async def authorization(
        cls,
        request: Request,
        user: UserModel | None,
        base_url: str,
        static_key: str,
    ) -> str:
        """Select either the stock static bearer or the user's OAuth session bearer.

        Args:
            request: Current HTTP request.
            user: Verified caller principal, if present.
            base_url: Active provider base URL for the media operation.
            static_key: Existing configured provider API key used when OAuth media is off.

        Returns:
            Formatted ``Authorization`` header value.
        """
        if not cls.is_enabled(request):
            return f'Bearer {static_key}'
        cls.authorized_base(request, base_url)
        return f'Bearer {await cls.session_token(request, user)}'

    @classmethod
    async def cache_scope(cls, request: Request, user: UserModel | None, base_url: str) -> str:
        """Authenticate a TTS cache lookup and compute a principal-and-session scope digest.

        Args:
            request: Current authenticated browser request.
            user: Verified caller principal.
            base_url: Active TTS OpenAI-compatible base URL.

        Returns:
            SHA-256 hex digest isolating cached audio by principal and OAuth login session.
        """
        cls.authorized_base(request, base_url)
        await cls.session_token(request, user)
        principal = user.id if user is not None else ''
        session_id = request.cookies.get('oauth_session_id', '')
        return hashlib.sha256((principal + '\0' + session_id).encode('utf-8')).hexdigest()

    @classmethod
    def private_speech_cache(cls, public_cache: Path) -> Path:
        """Return a mode-0700 private cache directory outside the shared speech cache tree.

        Args:
            public_cache: Open WebUI's public speech cache directory.

        Returns:
            Existing private directory path for OAuth-scoped speech responses.
        """
        root = public_cache.parents[2] if len(public_cache.parents) > 2 else public_cache.parent
        directory = root / 'gateway-media-private' / 'speech'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    @classmethod
    def validate_audio(cls, data: bytes, content_type: str) -> None:
        """Reject mislabeled, truncated, or raw PCM audio payloads from media backends.

        Args:
            data: Raw audio response bytes.
            content_type: Response ``Content-Type`` header value.

        Raises:
            HTTPException: The payload is not valid WAV or MP3 audio.
        """
        mime = content_type.split(';')[0].strip().lower()
        if mime in ('audio/wav', 'audio/x-wav', 'audio/wave'):
            cls._validate_wav(data)
            return
        if mime in ('audio/mpeg', 'audio/mp3'):
            cls._validate_mp3(data)
            return
        raise HTTPException(
            status_code=502,
            detail='System OAuth speech requires WAV or MP3 audio; raw PCM is not accepted.',
        )

    @classmethod
    def _validate_wav(cls, data: bytes) -> None:
        """Verify structural WAV headers and exact sample byte length.

        Args:
            data: Raw WAV response bytes.

        Raises:
            HTTPException: The payload is malformed or truncated WAV audio.
        """
        try:
            with wave.open(io.BytesIO(data), 'rb') as audio:
                frames = audio.getnframes()
                frame_size = audio.getnchannels() * audio.getsampwidth()
                if frames <= 0 or audio.getframerate() <= 0 or len(audio.readframes(frames)) != frames * frame_size:
                    raise ValueError('Invalid WAV frame payload')
        except (EOFError, ValueError, wave.Error):
            raise HTTPException(status_code=502, detail='Media backend returned invalid WAV audio.') from None

    @classmethod
    def _validate_mp3(cls, data: bytes) -> None:
        """Verify recognizable ID3 tag or MPEG frame sync header for MP3 audio.

        Args:
            data: Raw MP3 response bytes.

        Raises:
            HTTPException: The payload does not begin with a valid MP3 frame or ID3 tag.
        """
        has_id3 = data.startswith(b'ID3')
        has_frame_sync = len(data) >= 2 and data[0] == 255 and (data[1] & 224) == 224
        if not (has_id3 or has_frame_sync):
            raise HTTPException(status_code=502, detail='Media backend returned invalid MP3 audio.')

    @classmethod
    def image_target(cls, request: Request, url: str, base_url: str) -> str:
        """Canonicalize and authorize a managed PNG download URL on the gateway origin.

        Args:
            request: Current FastAPI HTTP request.
            url: Absolute or root-relative URL returned by the image endpoint.
            base_url: Configured OpenAI-compatible image provider base URL.

        Returns:
            Canonical absolute URL on the authorized gateway origin.

        Raises:
            HTTPException: The URL targets an unauthorized origin or noncanonical path.
        """
        trusted = urlsplit(cls.authorized_base(request, base_url))
        origin = f'{trusted.scheme}://{trusted.netloc}'
        target = origin + url if url.startswith('/') and not url.startswith('//') else url
        parsed = urlsplit(target)
        base_path = trusted.path.rstrip('/')
        allowed_prefixes = {'/v1'}
        if base_path:
            allowed_prefixes.add(base_path)
        matches_prefix = any(
            parsed.path.startswith(prefix)
            and IMAGE_FILE_SUFFIX_PATTERN.fullmatch(parsed.path.removeprefix(prefix)) is not None
            for prefix in allowed_prefixes
        )
        canonical_origin = parsed.scheme == trusted.scheme and parsed.netloc == trusted.netloc
        canonical_path = matches_prefix and target == origin + parsed.path
        if not canonical_origin or not canonical_path:
            raise HTTPException(
                status_code=502,
                detail='Media backend returned an unauthorized image download URL.',
            )
        return target

    @classmethod
    async def fetch_image_data(
        cls,
        request: Request,
        url: str,
        headers: dict[str, str],
        base_url: str,
    ) -> tuple[bytes, str]:
        """Download a managed PNG image from the authorized gateway without redirects.

        Args:
            request: Current FastAPI HTTP request.
            url: Managed image URL returned by the provider.
            headers: Outbound authorization headers excluding ``Content-Type``.
            base_url: Active OpenAI-compatible image provider base URL.

        Returns:
            Validated PNG image bytes and ``'image/png'`` MIME type.

        Raises:
            HTTPException: The upstream response is not a valid PNG image.
        """
        target = cls.image_target(request, url, base_url)
        session = await get_session()
        async with session.get(
            target,
            headers=headers,
            allow_redirects=False,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            response.raise_for_status()
            raw_content_type = response.headers.get('Content-Type')
            content_type = '' if raw_content_type is None else raw_content_type.split(';')[0].strip().lower()
            data = await response.read()
            if content_type != 'image/png' or not data.startswith(PNG_SIGNATURE):
                raise HTTPException(status_code=502, detail='Media backend returned an invalid managed PNG image.')
            return data, content_type
