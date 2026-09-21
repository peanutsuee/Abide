"""Cryptographic GitHub Actions OIDC verifier for backup-v2."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
from typing import Any
from urllib.parse import parse_qsl

import jwt
from jwt import PyJWKClient

from production_backup_capture import CaptureChannelError, V2_AUDIENCE


OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_JWKS_URL = f"{OIDC_ISSUER}/.well-known/jwks"
MAX_OIDC_TOKEN_BYTES = 8192
_TOKEN_FIELD_NAMES = {"token", "access_token", "authorization"}


class GitHubActionsBackupV2OidcVerifier:
    """Verify a GitHub Actions OIDC bearer token for the v2 backup audience."""

    def __init__(
        self,
        *,
        jwk_client: Any | None = None,
        jwk_client_factory: Callable[[], Any] | None = None,
        decoder: Callable[[str, Any], Mapping[str, Any]] | None = None,
    ) -> None:
        self._jwk_client = jwk_client
        self._jwk_client_factory = jwk_client_factory or (
            lambda: PyJWKClient(OIDC_JWKS_URL, cache_keys=True)
        )
        self._decoder = decoder or self._decode_token

    async def verify_request(self, request: Any) -> dict[str, Any]:
        token = _extract_bearer_token(request)
        await _reject_body_token_location(request)
        try:
            claims = await asyncio.to_thread(self._decoder, token, self._client())
        except CaptureChannelError:
            raise
        except Exception as exc:
            raise CaptureChannelError("oidc_denied") from exc
        if not isinstance(claims, Mapping) or claims.get("aud") != V2_AUDIENCE:
            raise CaptureChannelError("oidc_denied")
        return dict(claims)

    def _client(self) -> Any:
        if self._jwk_client is None:
            self._jwk_client = self._jwk_client_factory()
        return self._jwk_client

    @staticmethod
    def _decode_token(token: str, jwk_client: Any) -> Mapping[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise CaptureChannelError("oidc_denied")
            signing_key = jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=V2_AUDIENCE,
                issuer=OIDC_ISSUER,
                options={"require": ["exp", "iat", "nbf", "iss", "aud"]},
            )
        except CaptureChannelError:
            raise
        except Exception as exc:
            raise CaptureChannelError("oidc_denied") from exc
        if not isinstance(claims, Mapping):
            raise CaptureChannelError("oidc_denied")
        return claims


def _extract_bearer_token(request: Any) -> str:
    headers = list(getattr(request, "scope", {}).get("headers", ()))
    authorization_values = [
        value for key, value in headers if key.lower() == b"authorization"
    ]
    if len(authorization_values) != 1:
        raise CaptureChannelError("oidc_denied")
    value = authorization_values[0].decode("latin-1")
    if not value.startswith("Bearer "):
        raise CaptureChannelError("oidc_denied")
    token = value.removeprefix("Bearer ")
    if (
        not token
        or token != token.strip()
        or any(char.isspace() for char in token)
        or len(token.encode("utf-8")) > MAX_OIDC_TOKEN_BYTES
    ):
        raise CaptureChannelError("oidc_denied")
    _reject_header_and_query_token_locations(request, headers)
    return token


def _reject_header_and_query_token_locations(
    request: Any,
    headers: list[tuple[bytes, bytes]],
) -> None:
    query_string = getattr(request, "scope", {}).get("query_string", b"")
    try:
        query = query_string.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise CaptureChannelError("oidc_denied") from exc
    for key, _ in parse_qsl(query, keep_blank_values=True):
        if key.lower() in _TOKEN_FIELD_NAMES:
            raise CaptureChannelError("oidc_denied")
    for key, value in headers:
        if key.lower() != b"cookie":
            continue
        cookie_text = value.decode("latin-1").lower()
        if any(name in cookie_text for name in _TOKEN_FIELD_NAMES):
            raise CaptureChannelError("oidc_denied")


async def _reject_body_token_location(request: Any) -> None:
    parsed_body = getattr(request, "_json", None)
    if isinstance(parsed_body, Mapping):
        _reject_token_fields(parsed_body)
        return
    body_reader = getattr(request, "body", None)
    if body_reader is None:
        return
    try:
        body = await body_reader()
    except Exception as exc:
        raise CaptureChannelError("oidc_denied") from exc
    if not body:
        return
    if len(body) > MAX_OIDC_TOKEN_BYTES:
        raise CaptureChannelError("oidc_denied")
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise CaptureChannelError("oidc_denied") from exc
    headers = list(getattr(request, "scope", {}).get("headers", ()))
    content_type = ""
    for key, value in headers:
        if key.lower() == b"content-type":
            content_type = value.decode("latin-1").split(";", 1)[0].strip().lower()
            break
    if content_type == "application/json":
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise CaptureChannelError("oidc_denied") from exc
        if isinstance(parsed, Mapping):
            _reject_token_fields(parsed)
    elif content_type == "application/x-www-form-urlencoded":
        for key, _ in parse_qsl(text, keep_blank_values=True):
            if key.lower() in _TOKEN_FIELD_NAMES:
                raise CaptureChannelError("oidc_denied")


def _reject_token_fields(value: Mapping[Any, Any]) -> None:
    if any(str(key).lower() in _TOKEN_FIELD_NAMES for key in value):
        raise CaptureChannelError("oidc_denied")
