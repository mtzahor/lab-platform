from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Protocol

_KEYCHAIN_SERVICE = "lab-platform-cli"
_KEYCHAIN_LABEL = "Lab Platform CLI session"


class CredentialStoreError(RuntimeError):
    """A native credential-store operation could not be completed safely."""


class CredentialStoreUnavailableError(CredentialStoreError):
    """The operating system does not expose a supported credential-store command."""


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """A refreshable interactive login stored as one native secret."""

    access_token: str = dataclass_field(repr=False)
    refresh_token: str | None = dataclass_field(default=None, repr=False)
    session_id: str | None = None
    expires_at: str | None = None
    maximum_expires_at: str | None = None

    def __post_init__(self) -> None:
        _validate_token(self.access_token, "Access token")
        if self.refresh_token is not None:
            _validate_token(self.refresh_token, "Refresh token")
        for name, value in (
            ("Session ID", self.session_id),
            ("Session expiry", self.expires_at),
            ("Maximum session expiry", self.maximum_expires_at),
        ):
            if value is not None:
                _validate_metadata(value, name)

    @classmethod
    def from_auth_response(
        cls,
        payload: dict[str, object],
        *,
        previous: StoredCredential | None = None,
    ) -> StoredCredential:
        """Build a stored login from login/refresh JSON without retaining public identity data."""

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("Invalid authentication response: missing access_token")

        explicit_refresh = payload.get("refresh_token")
        if explicit_refresh is not None and not isinstance(explicit_refresh, str):
            raise ValueError("Invalid authentication response: refresh_token must be a string")
        if isinstance(explicit_refresh, str):
            refresh_token = explicit_refresh
        elif (
            previous is not None
            and previous.refresh_token is not None
            and previous.refresh_token != previous.access_token
        ):
            # A future server may issue a stable, distinct refresh token and omit it from
            # subsequent responses. Keep that token unless the server explicitly rotates it.
            refresh_token = previous.refresh_token
        else:
            # The current control-plane session is a rotating opaque token: that same token is
            # accepted by /auth/refresh after its short access lifetime has elapsed.
            refresh_token = access_token

        session = payload.get("session")
        session_data = session if isinstance(session, dict) else {}
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            session_id=_optional_string(session_data.get("id"), previous, "session_id"),
            expires_at=_optional_string(
                payload.get("expires_at", session_data.get("expires_at")),
                previous,
                "expires_at",
            ),
            maximum_expires_at=_optional_string(
                session_data.get("maximum_expires_at"),
                previous,
                "maximum_expires_at",
            ),
        )

    @classmethod
    def decode(cls, secret: str) -> StoredCredential:
        """Decode the versioned bundle, accepting legacy access-token-only entries."""

        if not secret.startswith("{"):
            return cls(access_token=secret, refresh_token=secret)
        try:
            payload = json.loads(secret)
        except json.JSONDecodeError as exc:
            raise CredentialStoreError("The stored Lab Platform credential is malformed.") from exc
        if not isinstance(payload, dict):
            raise CredentialStoreError("The stored Lab Platform credential is malformed.")
        if payload.get("format") != "lab-platform-session" or payload.get("version") != 1:
            raise CredentialStoreError("The stored Lab Platform credential format is unsupported.")
        try:
            access_token = payload["access_token"]
            refresh_token = payload.get("refresh_token")
            if not isinstance(access_token, str):
                raise ValueError("access token")
            if refresh_token is not None and not isinstance(refresh_token, str):
                raise ValueError("refresh token")
            return cls(
                access_token=access_token,
                refresh_token=refresh_token,
                session_id=_stored_optional_string(payload, "session_id"),
                expires_at=_stored_optional_string(payload, "expires_at"),
                maximum_expires_at=_stored_optional_string(payload, "maximum_expires_at"),
            )
        except (KeyError, ValueError) as exc:
            raise CredentialStoreError("The stored Lab Platform credential is malformed.") from exc

    def encode(self) -> str:
        """Serialize the complete login for one Keychain/Secret Service password value."""

        return json.dumps(
            {
                "format": "lab-platform-session",
                "version": 1,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "session_id": self.session_id,
                "expires_at": self.expires_at,
                "maximum_expires_at": self.maximum_expires_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class CredentialStore(Protocol):
    """Minimal server-scoped storage used by ``labctl auth``."""

    def load(self, server: str) -> StoredCredential | None:
        """Return the interactive login stored for ``server``, if any."""

    def save(self, server: str, credential: StoredCredential) -> None:
        """Store the complete login for ``server`` in the native credential store."""

    def delete(self, server: str) -> bool:
        """Delete the interactive login for ``server`` and report whether one existed."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]


class NativeCredentialStore:
    """Use macOS Keychain or Linux Secret Service without persisting plaintext files."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        executable_finder: ExecutableFinder | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        current_platform = sys.platform if platform is None else platform
        find_executable = shutil.which if executable_finder is None else executable_finder
        self._runner = subprocess.run if runner is None else runner
        self._backend: str | None = None
        self._executable: str | None = None

        if current_platform == "darwin":
            executable = find_executable("security")
            if executable is not None:
                self._backend = "macos"
                self._executable = executable
        elif current_platform.startswith("linux"):
            executable = find_executable("secret-tool")
            if executable is not None:
                self._backend = "linux"
                self._executable = executable

    def load(self, server: str) -> StoredCredential | None:
        account = _server_account(server)
        backend, executable = self._require_backend()
        if backend == "macos":
            command = [
                executable,
                "find-generic-password",
                "-a",
                account,
                "-s",
                _KEYCHAIN_SERVICE,
                "-w",
            ]
        else:
            command = [
                executable,
                "lookup",
                "service",
                _KEYCHAIN_SERVICE,
                "server",
                account,
            ]
        completed = self._run(command)
        if completed.returncode != 0:
            if _is_missing_credential(backend, completed):
                return None
            raise CredentialStoreError(_command_failure(backend, "read", completed.stderr))
        secret = completed.stdout.rstrip("\r\n")
        if not secret:
            return None
        return StoredCredential.decode(secret)

    def save(self, server: str, credential: StoredCredential) -> None:
        account = _server_account(server)
        secret = credential.encode()
        backend, executable = self._require_backend()
        if backend == "macos":
            # Keeping ``-w`` last makes `security` read the password from stdin instead of
            # placing it in argv, where process listings could expose it.
            command = [
                executable,
                "add-generic-password",
                "-a",
                account,
                "-s",
                _KEYCHAIN_SERVICE,
                "-l",
                _KEYCHAIN_LABEL,
                "-U",
                "-w",
            ]
        else:
            command = [
                executable,
                "store",
                f"--label={_KEYCHAIN_LABEL}",
                "service",
                _KEYCHAIN_SERVICE,
                "server",
                account,
            ]
        completed = self._run(command, secret=secret)
        if completed.returncode != 0:
            raise CredentialStoreError(
                _command_failure(
                    backend,
                    "write",
                    completed.stderr,
                    secrets=(secret, credential.access_token, credential.refresh_token),
                )
            )

    def delete(self, server: str) -> bool:
        account = _server_account(server)
        backend, executable = self._require_backend()
        if backend == "macos":
            command = [
                executable,
                "delete-generic-password",
                "-a",
                account,
                "-s",
                _KEYCHAIN_SERVICE,
            ]
        else:
            command = [
                executable,
                "clear",
                "service",
                _KEYCHAIN_SERVICE,
                "server",
                account,
            ]
        completed = self._run(command)
        if completed.returncode == 0:
            return True
        if _is_missing_credential(backend, completed):
            return False
        raise CredentialStoreError(_command_failure(backend, "delete", completed.stderr))

    def _require_backend(self) -> tuple[str, str]:
        if self._backend is None or self._executable is None:
            raise CredentialStoreUnavailableError(_unavailable_message())
        return self._backend, self._executable

    def _run(
        self,
        command: Sequence[str],
        *,
        secret: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                list(command),
                input=None if secret is None else f"{secret}\n",
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise CredentialStoreUnavailableError(
                f"Native credential storage could not be started: {exc}. {_unavailable_message()}"
            ) from exc


def _server_account(server: str) -> str:
    account = server.strip().rstrip("/")
    if not account:
        raise ValueError("Server URL must be non-empty")
    if any(character in account for character in ("\x00", "\r", "\n")):
        raise ValueError("Server URL cannot contain NUL or newline characters")
    return account


def _validate_token(token: str, label: str) -> None:
    if not token.strip():
        raise ValueError(f"{label} must be non-empty")
    if any(character in token for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} cannot contain NUL or newline characters")


def _validate_metadata(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty")
    if len(value) > 2000 or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} is invalid")


def _optional_string(
    value: object,
    previous: StoredCredential | None,
    field: str,
) -> str | None:
    if value is None:
        return getattr(previous, field) if previous is not None else None
    if not isinstance(value, str):
        raise ValueError(f"Invalid authentication response: {field} must be a string")
    return value


def _stored_optional_string(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(field)
    return value


def _is_missing_credential(
    backend: str,
    completed: subprocess.CompletedProcess[str],
) -> bool:
    message = completed.stderr.casefold()
    if backend == "macos":
        return completed.returncode == 44 or "could not be found" in message
    return completed.returncode == 1 and (not message.strip() or "not found" in message)


def _command_failure(
    backend: str,
    operation: str,
    stderr: str,
    *,
    secrets: Sequence[str | None] = (),
) -> str:
    store_name = "macOS Keychain" if backend == "macos" else "Linux Secret Service"
    detail = stderr.strip()
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[redacted]")
    suffix = f": {detail}" if detail else ""
    return f"Could not {operation} the Lab Platform credential in {store_name}{suffix}"


def _unavailable_message() -> str:
    return (
        "No supported OS credential store is available. On macOS, ensure the `security` "
        "command is available. On Linux, install `secret-tool` and configure Secret Service. "
        "Alternatively, authenticate non-interactively with LAB_PLATFORM_TOKEN."
    )
