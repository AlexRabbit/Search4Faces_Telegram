"""JSON-RPC client for search4faces.com with optional offline mock."""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SEARCH4FACES_DEFAULT_URL = "https://search4faces.com/api/json-rpc/v1"

DEFAULT_SOURCES = (
    "vk_wall",
    "vkok_avatar",
    "tt_avatar",
    "ch_avatar",
    "vkokn_avatar",
    "sb_photo",
)


class Search4FacesError(Exception):
    """API or transport failure."""


class Search4FacesClient:
    def __init__(
        self,
        api_key: str,
        api_url: str = SEARCH4FACES_DEFAULT_URL,
        *,
        mock: bool = False,
        timeout: float = 121.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_url = api_url.rstrip("/")
        self.mock = mock
        self.timeout = timeout

    async def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.mock:
            return _mock_json_rpc(method, params)

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": str(uuid.uuid4()),
            "params": params,
        }
        headers = {
            "Content-Type": "application/json",
            "x-authorization-token": self.api_key,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(self.api_url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise Search4FacesError(f"{err.get('code')}: {err.get('message')}")
        if "result" not in data:
            raise Search4FacesError("Invalid JSON-RPC response (no result)")
        return data["result"]

    async def rate_limit(self) -> dict[str, Any]:
        return await self._post("rateLimit", {})

    async def detect_faces(self, image_bytes: bytes) -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return await self._post("detectFaces", {"image": b64})

    async def search_face(
        self,
        image_id: str,
        face: dict[str, Any],
        *,
        source: str,
        results: int = 10,
        hidden: bool = True,
        lang: str = "en",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "image": image_id,
            "face": face,
            "source": source,
            "hidden": hidden,
            "results": results,
            "lang": lang,
        }
        result = await self._post("searchFace", params)
        profiles = result.get("profiles") or []
        return profiles


def _mock_json_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "rateLimit":
        return {
            "apikey": "mock-mode",
            "limit": 999999,
            "remaining": 999999,
            "enddate": "2099-01-01 00:00:00",
            "speed": 999,
            "allowed": ["rateLimit", "detectFaces", "searchFace"],
            "disabled": "no",
        }
    if method == "detectFaces":
        return {
            "image": "mock.upload.jpg",
            "faces": [
                {
                    "x": 20,
                    "y": 25,
                    "width": 60,
                    "height": 72,
                    "lm1_x": 35,
                    "lm1_y": 48,
                    "lm2_x": 58,
                    "lm2_y": 46,
                    "lm3_x": 48,
                    "lm3_y": 58,
                    "lm4_x": 40,
                    "lm4_y": 68,
                    "lm5_x": 58,
                    "lm5_y": 66,
                }
            ],
        }
    if method == "searchFace":
        source = params.get("source", "vk_wall")
        base = f"https://example.com/mock/{source}"
        profiles = []
        for i in range(3):
            profiles.append(
                {
                    "score": str(95.5 - i * 2.1),
                    "face": f"https://picsum.photos/seed/t4f{i}{source}/220/220",
                    "profile": f"{base}/profile/{i + 1}",
                    "photo": f"{base}/photo/{i + 1}",
                    "photo_x": 10,
                    "photo_y": 10,
                    "photo_width": 200,
                    "photo_height": 200,
                    "source": f"https://picsum.photos/seed/t4fsrc{i}{source}/400/300",
                    "age": 28 + i,
                    "first_name": "Mock",
                    "last_name": f"User{i + 1}",
                    "maiden_name": "",
                    "city": "Demo City",
                    "country": "Mockland",
                }
            )
        return {"profiles": profiles}

    raise Search4FacesError(f"Unknown method in mock: {method}")


__all__ = [
    "DEFAULT_SOURCES",
    "SEARCH4FACES_DEFAULT_URL",
    "Search4FacesClient",
    "Search4FacesError",
]
