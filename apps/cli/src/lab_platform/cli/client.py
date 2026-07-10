from __future__ import annotations

import json
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AgentConnectionError(RuntimeError):
    """Raised when labctl cannot read a response from the local agent."""


class AgentClient:
    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def get(self, path: str) -> object:
        request = Request(f"{self._base_url}/{path.lstrip('/')}", method="GET")
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return cast(object, json.load(response))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AgentConnectionError(f"Could not read {request.full_url}: {exc}") from exc
