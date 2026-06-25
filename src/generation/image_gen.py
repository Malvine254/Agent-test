"""Image generation backend (FLUX primary, Azure DALL-E fallback).

Tries providers in ``Config.IMAGE_PROVIDER_ORDER`` and returns the raw image
bytes for the first that succeeds. Network/credential errors fall through to the
next provider; if all fail, raises ``ImageGenError`` with a readable message.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

_TIMEOUT = 120


class ImageGenError(RuntimeError):
    pass


def _decode_response(payload: dict) -> Optional[bytes]:
    data = (payload or {}).get("data") or []
    if not data:
        return None
    first = data[0] or {}
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        resp = requests.get(first["url"], timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.content
    return None


def _generations_url(endpoint: str, deployment: str, api_version: str) -> str:
    base = (endpoint or "").rstrip("/")
    return f"{base}/openai/deployments/{deployment}/images/generations?api-version={api_version}"


def _flux(prompt: str, size: str) -> Optional[bytes]:
    if not (Config.FLUX_ENDPOINT and Config.FLUX_API_KEY):
        return None
    url = _generations_url(Config.FLUX_ENDPOINT, Config.FLUX_DEPLOYMENT, Config.FLUX_API_VERSION)
    resp = requests.post(
        url,
        headers={"api-key": Config.FLUX_API_KEY, "Content-Type": "application/json"},
        json={"prompt": prompt, "n": 1, "size": size},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return _decode_response(resp.json())


def _dalle(prompt: str, size: str) -> Optional[bytes]:
    if not (Config.AZURE_DALLE_ENDPOINT and Config.AZURE_DALLE_API_KEY):
        return None
    url = _generations_url(Config.AZURE_DALLE_ENDPOINT, Config.AZURE_DALLE_DEPLOYMENT,
                           Config.AZURE_DALLE_API_VERSION)
    # gpt-image-1 does not accept 'style'; omit it for compatibility with both
    # gpt-image-1 and dall-e-3 (style defaults to vivid anyway).
    resp = requests.post(
        url,
        headers={"api-key": Config.AZURE_DALLE_API_KEY, "Content-Type": "application/json"},
        json={"prompt": prompt, "n": 1, "size": size, "quality": "standard"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return _decode_response(resp.json())


_PROVIDERS = {"flux": _flux, "dalle": _dalle}

# Sizes accepted by both backends; everything else is coerced to the default.
_VALID_SIZES = {"1024x1024", "1792x1024", "1024x1792"}


def generate_image(prompt: str, *, size: str = "1024x1024") -> tuple[bytes, str]:
    """Generate an image. Returns (png_bytes, provider_name).

    Raises ImageGenError if no provider is configured or all providers fail.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageGenError("An image description (prompt) is required.")
    if size not in _VALID_SIZES:
        size = "1024x1024"

    order = Config.IMAGE_PROVIDER_ORDER or ["flux", "dalle"]
    errors: list[str] = []
    any_configured = False
    for name in order:
        fn = _PROVIDERS.get(name)
        if not fn:
            continue
        try:
            data = fn(prompt, size)
        except Exception as exc:
            any_configured = True
            logger.warning("Image provider %s failed: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if data:
            any_configured = True
            logger.info("Image generated via %s (%d bytes)", name, len(data))
            return data, name
        errors.append(f"{name}: empty response")

    if not any_configured:
        raise ImageGenError("Image generation is not configured (no FLUX or DALL-E credentials).")
    raise ImageGenError("Image generation failed for all providers (" + "; ".join(errors) + ").")
