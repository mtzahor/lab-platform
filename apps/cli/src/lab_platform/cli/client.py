from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class AgentConnectionError(RuntimeError):
    """Raised when labctl cannot connect to the configured Agent."""


class AgentApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class AgentClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def get(self, path: str, query: dict[str, object] | None = None) -> object:
        url = self._url(path)
        if query:
            values = {key: value for key, value in query.items() if value is not None}
            if values:
                url = f"{url}?{urlencode(values, doseq=True)}"
        return self._request(Request(url, method="GET"))

    def post(self, path: str, payload: dict[str, object]) -> object:
        return self._json_request(path, "POST", payload)

    def delete(self, path: str, payload: dict[str, object]) -> object | None:
        return self._json_request(path, "DELETE", payload)

    def upload(
        self,
        path: str,
        firmware_path: Path,
        *,
        owner: str,
        version: str | None,
    ) -> object:
        boundary = f"lab-platform-{uuid4().hex}"
        body = bytearray()

        def field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(value.encode())
            body.extend(b"\r\n")

        field("owner", owner)
        if version is not None:
            field("version", version)
        content_type = mimetypes.guess_type(firmware_path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="firmware"; '
                f'filename="{firmware_path.name}"\r\n'
            ).encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(firmware_path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = Request(
            self._url(path),
            data=bytes(body),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._request(request)

    def _json_request(self, path: str, method: str, payload: dict[str, object]) -> object | None:
        request = Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        return self._request(request)

    def _request(self, request: Request) -> object | None:
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                if response.status == 204:
                    return None
                return cast(object, json.load(response))
        except HTTPError as exc:
            raise self._api_error(exc) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AgentConnectionError(f"Could not read {request.full_url}: {exc}") from exc

    def _api_error(self, error: HTTPError) -> AgentApiError:
        try:
            payload = json.loads(error.read())
            body = payload.get("error", {}) if isinstance(payload, dict) else {}
            if not isinstance(body, dict):
                body = {}
            details = body.get("details")
            return AgentApiError(
                error.code,
                str(body.get("code", "HTTP_ERROR")),
                str(body.get("message", error.reason)),
                cast(dict[str, object], details) if isinstance(details, dict) else {},
            )
        except (json.JSONDecodeError, OSError):
            return AgentApiError(error.code, "HTTP_ERROR", str(error.reason))

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"
