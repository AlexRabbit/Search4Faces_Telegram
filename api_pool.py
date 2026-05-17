"""Round-robin Search4Faces clients over multiple API keys."""

from __future__ import annotations

import itertools
import threading
from typing import Iterator

from search4faces_client import SEARCH4FACES_DEFAULT_URL, Search4FacesClient

from bot_storage import load_api_keys, mask_api_key


class ApiKeyPool:
    def __init__(
        self,
        keys: list[str],
        *,
        api_url: str = SEARCH4FACES_DEFAULT_URL,
        mock: bool = False,
    ) -> None:
        self._keys = list(keys)
        self._api_url = api_url.rstrip("/")
        self._mock = mock
        self._lock = threading.Lock()
        self._cycle: Iterator[int] = itertools.cycle(range(max(1, len(self._keys) or 1)))

    def reload(self, keys: list[str] | None = None, *, mock: bool | None = None) -> None:
        with self._lock:
            if keys is not None:
                self._keys = list(keys)
            if mock is not None:
                self._mock = mock
            n = max(1, len(self._keys) or 1)
            self._cycle = itertools.cycle(range(n))

    @property
    def mock(self) -> bool:
        return self._mock or not self._keys

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def masked_keys(self) -> list[str]:
        return [mask_api_key(k) for k in self._keys]

    def next_client(self) -> Search4FacesClient:
        with self._lock:
            if self._mock or not self._keys:
                return Search4FacesClient("mock", api_url=self._api_url, mock=True)
            idx = next(self._cycle)
            key = self._keys[idx % len(self._keys)]
        return Search4FacesClient(key, api_url=self._api_url, mock=False)

    def client_for_index(self, index: int) -> Search4FacesClient:
        if self._mock or not self._keys:
            return Search4FacesClient("mock", api_url=self._api_url, mock=True)
        key = self._keys[index % len(self._keys)]
        return Search4FacesClient(key, api_url=self._api_url, mock=False)


def build_api_pool(api_url: str, *, force_mock: bool = False) -> ApiKeyPool:
    keys = load_api_keys()
    mock = force_mock or not keys
    return ApiKeyPool(keys, api_url=api_url, mock=mock)
