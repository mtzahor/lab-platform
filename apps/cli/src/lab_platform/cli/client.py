from __future__ import annotations

import json
import mimetypes
from collections.abc import Mapping
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
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        if token is not None and (not token.strip() or "\r" in token or "\n" in token):
            raise ValueError("API token must be non-empty and cannot contain newlines")
        self._token = token.strip() if token is not None else None

    def get(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        request = Request(
            self._url_with_query(path, query),
            method="GET",
            headers=self._headers(headers),
        )
        return self._request(request)

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        return self._json_request(
            path,
            "POST",
            payload,
            headers=headers,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    def delete(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object | None:
        return self._json_request(
            path,
            "DELETE",
            payload,
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def upload(
        self,
        path: str,
        firmware_path: Path,
        *,
        owner: str,
        version: str | None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        fields: dict[str, str] = {"owner": owner}
        if version is not None:
            fields["version"] = version
        return self._upload_file(
            path,
            firmware_path,
            file_field="firmware",
            fields=fields,
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def upload_artifact(
        self,
        path: str,
        artifact_path: Path,
        *,
        name: str | None = None,
        artifact_type: str | None = None,
        ci_session_id: str | None = None,
        checksum: str | None = None,
        fields: Mapping[str, str | None] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        """Upload a CI artifact using the provider-neutral multipart API."""

        form_fields: dict[str, str] = {
            key: value for key, value in (fields or {}).items() if value is not None
        }
        optional_fields = {
            "name": name,
            "artifact_type": artifact_type,
            "ci_session_id": ci_session_id,
            "sha256": checksum,
        }
        for key, value in optional_fields.items():
            if value is not None:
                form_fields[key] = value
        return self._upload_file(
            path,
            artifact_path,
            file_field="file",
            fields=form_fields,
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def download(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Download a binary response without attempting JSON decoding."""

        request = Request(
            self._url_with_query(path, query),
            method="GET",
            headers=self._headers(headers),
        )
        return self._request_bytes(request)

    def get_text(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Read a plain-text response such as JUnit XML or streamed output."""

        return self.download(path, query, headers=headers).decode(encoding)

    def _upload_file(
        self,
        path: str,
        file_path: Path,
        *,
        file_field: str,
        fields: Mapping[str, str],
        headers: Mapping[str, str] | None,
        idempotency_key: str | None,
    ) -> object:
        boundary = f"lab-platform-{uuid4().hex}"
        body = _multipart_body(boundary, file_path, file_field=file_field, fields=fields)
        request = Request(
            self._url(path),
            data=body,
            method="POST",
            headers=self._headers(
                headers,
                content_type=f"multipart/form-data; boundary={boundary}",
                idempotency_key=idempotency_key,
            ),
        )
        return self._request(request)

    def _json_request(
        self,
        path: str,
        method: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None,
        idempotency_key: str | None,
        timeout: float | None = None,
    ) -> object | None:
        request = Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers=self._headers(
                headers,
                content_type="application/json",
                idempotency_key=idempotency_key,
            ),
        )
        return self._request(request, timeout=timeout)

    def _request(self, request: Request, *, timeout: float | None = None) -> object | None:
        try:
            with urlopen(
                request,
                timeout=self._timeout if timeout is None else timeout,
            ) as response:  # noqa: S310
                if response.status == 204:
                    return None
                return cast(object, json.load(response))
        except HTTPError as exc:
            raise self._api_error(exc) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AgentConnectionError(f"Could not read {request.full_url}: {exc}") from exc

    def _request_bytes(self, request: Request) -> bytes:
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return cast(bytes, response.read())
        except HTTPError as exc:
            raise self._api_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
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

    def _url_with_query(self, path: str, query: dict[str, object] | None) -> str:
        url = self._url(path)
        if not query:
            return url
        values = {key: value for key, value in query.items() if value is not None}
        return f"{url}?{urlencode(values, doseq=True)}" if values else url

    def _headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        result = dict(headers or {})
        if content_type is not None:
            _set_header(result, "Content-Type", content_type)
        if idempotency_key is not None:
            _set_header(result, "Idempotency-Key", idempotency_key)
        if self._token is not None:
            _set_header(result, "Authorization", f"Bearer {self._token}")
        return result


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    for existing in tuple(headers):
        if existing.casefold() == name.casefold():
            headers.pop(existing)
    headers[name] = value


def _multipart_body(
    boundary: str,
    file_path: Path,
    *,
    file_field: str,
    fields: Mapping[str, str],
) -> bytes:
    body = bytearray()
    for name, value in fields.items():
        safe_name = _multipart_value(name)
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    filename = _multipart_value(file_path.name)
    safe_file_field = _multipart_value(file_field)
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="{safe_file_field}"; filename="{filename}"\r\n'
        ).encode()
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
    body.extend(file_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(body)


def _multipart_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
