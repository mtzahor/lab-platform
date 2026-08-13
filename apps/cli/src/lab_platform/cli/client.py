from __future__ import annotations

import json
import mimetypes
import threading
from collections.abc import Callable, Mapping
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
        refresh_token: str | None = None,
        on_session_refresh: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token = _validated_token(token, "API token")
        self._refresh_token = _validated_token(refresh_token, "Refresh token")
        if self._refresh_token is not None and self._token is None:
            raise ValueError("Refresh token requires an API token")
        self._on_session_refresh = on_session_refresh
        self._refresh_lock = threading.Lock()

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

    def put(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object | None:
        return self._json_request(
            path,
            "PUT",
            payload,
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def patch(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object | None:
        return self._json_request(
            path,
            "PATCH",
            payload,
            headers=headers,
            idempotency_key=idempotency_key,
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
        return self._request_json(request, timeout=timeout, allow_refresh=True)

    def _request_json(
        self,
        request: Request,
        *,
        timeout: float | None,
        allow_refresh: bool,
    ) -> object | None:
        try:
            with urlopen(
                request,
                timeout=self._timeout if timeout is None else timeout,
            ) as response:  # noqa: S310
                if response.status == 204:
                    return None
                return cast(object, json.load(response))
        except HTTPError as exc:
            error = self._api_error(exc)
            failed_access_token = _request_bearer_token(request)
            if allow_refresh and self._token_rotated_since(failed_access_token, error):
                return self._request_json(
                    self._request_with_current_token(request),
                    timeout=timeout,
                    allow_refresh=False,
                )
            if allow_refresh and self._should_refresh(error):
                self._refresh_session(failed_access_token)
                return self._request_json(
                    self._request_with_current_token(request),
                    timeout=timeout,
                    allow_refresh=False,
                )
            raise error from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AgentConnectionError(f"Could not read {request.full_url}: {exc}") from exc

    def _request_bytes(self, request: Request) -> bytes:
        return self._request_binary(request, allow_refresh=True)

    def _request_binary(self, request: Request, *, allow_refresh: bool) -> bytes:
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return cast(bytes, response.read())
        except HTTPError as exc:
            error = self._api_error(exc)
            failed_access_token = _request_bearer_token(request)
            if allow_refresh and self._token_rotated_since(failed_access_token, error):
                return self._request_binary(
                    self._request_with_current_token(request),
                    allow_refresh=False,
                )
            if allow_refresh and self._should_refresh(error):
                self._refresh_session(failed_access_token)
                return self._request_binary(
                    self._request_with_current_token(request),
                    allow_refresh=False,
                )
            raise error from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AgentConnectionError(f"Could not read {request.full_url}: {exc}") from exc

    def _should_refresh(self, error: AgentApiError) -> bool:
        return (
            error.status == 401
            and error.code in {"SESSION_EXPIRED", "TOKEN_EXPIRED"}
            and self._refresh_token is not None
        )

    def _token_rotated_since(
        self,
        failed_access_token: str | None,
        error: AgentApiError,
    ) -> bool:
        return (
            error.status == 401
            and failed_access_token is not None
            and self._token is not None
            and failed_access_token != self._token
        )

    def _refresh_session(self, failed_access_token: str | None) -> None:
        with self._refresh_lock:
            if failed_access_token is not None and failed_access_token != self._token:
                # Another request already rotated the session while this one was waiting.
                return
            refresh_token = self._refresh_token
            if refresh_token is None:  # pragma: no cover - guarded by _should_refresh
                raise AssertionError("session refresh requires a refresh token")
            request = Request(
                self._url("/api/v1/auth/refresh"),
                data=b"{}",
                method="POST",
                headers={
                    "Authorization": f"Bearer {refresh_token}",
                    "Content-Type": "application/json",
                },
            )
            payload = self._read_refresh_response(request)
            access_token = payload.get("access_token")
            if not isinstance(access_token, str):
                raise AgentConnectionError(
                    "The control plane returned an invalid session refresh response."
                )
            new_access_token = _validated_token(access_token, "API token")
            if new_access_token is None:  # pragma: no cover - validated non-optional input
                raise AssertionError("validated refresh response token is non-null")
            supplied_refresh_token = payload.get("refresh_token")
            if supplied_refresh_token is not None and not isinstance(supplied_refresh_token, str):
                raise AgentConnectionError(
                    "The control plane returned an invalid session refresh response."
                )
            if isinstance(supplied_refresh_token, str):
                new_refresh_token = _validated_token(supplied_refresh_token, "Refresh token")
            elif refresh_token == self._token:
                # Current control planes rotate a single opaque access/refresh token.
                new_refresh_token = new_access_token
            else:
                # Preserve a future server's distinct stable refresh token when omitted.
                new_refresh_token = refresh_token
            self._token = new_access_token
            self._refresh_token = new_refresh_token
            if self._on_session_refresh is not None:
                try:
                    self._on_session_refresh(payload)
                except Exception:
                    # The old token was invalidated by rotation. Do not leave a live session
                    # whose replacement token could not be persisted safely.
                    self._best_effort_logout()
                    raise

    def _read_refresh_response(self, request: Request) -> dict[str, object]:
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = json.load(response)
        except HTTPError as exc:
            raise self._api_error(exc) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AgentConnectionError(
                f"Could not refresh the session with {request.full_url}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise AgentConnectionError(
                "The control plane returned an invalid session refresh response."
            )
        return cast(dict[str, object], payload)

    def _best_effort_logout(self) -> None:
        if self._token is None:  # pragma: no cover - refresh invariant
            return
        request = Request(
            self._url("/api/v1/auth/logout"),
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout):  # noqa: S310
                pass
        except (HTTPError, URLError, TimeoutError, OSError):
            pass

    def _request_with_current_token(self, request: Request) -> Request:
        headers = {
            name: value
            for name, value in request.header_items()
            if name.casefold() != "authorization"
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return Request(
            request.full_url,
            data=request.data,
            method=request.get_method(),
            headers=headers,
        )

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


def _validated_token(token: str | None, label: str) -> str | None:
    if token is None:
        return None
    if not token.strip() or "\r" in token or "\n" in token:
        raise ValueError(f"{label} must be non-empty and cannot contain newlines")
    return token.strip()


def _request_bearer_token(request: Request) -> str | None:
    header = request.get_header("Authorization")
    if header is None:
        return None
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return None
    return token


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
