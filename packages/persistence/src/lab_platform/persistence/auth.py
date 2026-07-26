from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import UUID

from lab_platform.models import ApiToken, ApiTokenScope
from lab_platform.persistence.database import SQLiteDatabase


class SQLiteApiTokenRepository:
    """SQLite-backed API-token metadata.

    Only token hashes cross this boundary. Plaintext credentials are never accepted
    or persisted by the repository.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, token: ApiToken) -> ApiToken:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO api_tokens "
                "(id, name, token_hash, owner, scopes_json, created_at, expires_at, "
                "revoked_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _token_values(token),
            )
        return token

    async def get(self, token_id: UUID) -> ApiToken | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (str(token_id),)
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def lookup(self, token_hash: str) -> ApiToken | None:
        """Compatibility alias used by authentication services."""

        return await self.get_by_hash(token_hash)

    async def list(
        self,
        *,
        owner: str | None = None,
        include_revoked: bool = True,
        limit: int = 500,
    ) -> list[ApiToken]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        conditions: list[str] = []
        values: list[object] = []
        if owner is not None:
            conditions.append("owner = ?")
            values.append(owner)
        if not include_revoked:
            conditions.append("revoked_at IS NULL")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_tokens{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_token_from_row(row) for row in rows]

    async def update(self, token: ApiToken) -> ApiToken:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE api_tokens SET name = ?, token_hash = ?, owner = ?, scopes_json = ?, "
                "created_at = ?, expires_at = ?, revoked_at = ?, last_used_at = ? WHERE id = ?",
                (*_token_values(token)[1:], str(token.id)),
            )
        if cursor.rowcount != 1:
            raise LookupError(f"API token {token.id} does not exist")
        return token

    async def revoke(self, token_id: UUID, revoked_at: datetime) -> ApiToken | None:
        timestamp = revoked_at.isoformat()
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (timestamp, str(token_id)),
            )
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (str(token_id),)
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def update_last_used(self, token_id: UUID, used_at: datetime) -> ApiToken | None:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE api_tokens SET last_used_at = CASE "
                "WHEN last_used_at IS NULL OR last_used_at < ? THEN ? ELSE last_used_at END "
                "WHERE id = ? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (
                    used_at.isoformat(),
                    used_at.isoformat(),
                    str(token_id),
                    used_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (str(token_id),)
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def delete(self, token_id: UUID) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM api_tokens WHERE id = ?", (str(token_id),))
        return cursor.rowcount == 1


def _token_values(token: ApiToken) -> tuple[object, ...]:
    scopes = sorted(
        scope.value if isinstance(scope, ApiTokenScope) else str(scope) for scope in token.scopes
    )
    return (
        str(token.id),
        token.name,
        token.token_hash,
        token.owner,
        json.dumps(scopes, separators=(",", ":")),
        token.created_at.isoformat(),
        _datetime_value(token.expires_at),
        _datetime_value(token.revoked_at),
        _datetime_value(token.last_used_at),
    )


def _token_from_row(row: sqlite3.Row) -> ApiToken:
    return ApiToken(
        id=UUID(row["id"]),
        name=row["name"],
        token_hash=row["token_hash"],
        owner=row["owner"],
        scopes={ApiTokenScope(item) for item in json.loads(row["scopes_json"])},
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        revoked_at=_parse_datetime(row["revoked_at"]),
        last_used_at=_parse_datetime(row["last_used_at"]),
    )


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
