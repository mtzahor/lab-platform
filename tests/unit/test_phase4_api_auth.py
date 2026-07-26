from __future__ import annotations

import pytest
from lab_platform.agent.api.auth import bearer_token
from lab_platform.core.errors import AuthenticationRequiredError


@pytest.mark.parametrize("value", [None, "", "Basic secret", "Bearer", "Bearer   "])
def test_bearer_token_rejects_missing_or_malformed_headers(value: str | None) -> None:
    with pytest.raises(AuthenticationRequiredError):
        bearer_token(value)


def test_bearer_token_is_case_insensitive_and_trims_value() -> None:
    assert bearer_token("bEaReR   secret-token ") == "secret-token"
