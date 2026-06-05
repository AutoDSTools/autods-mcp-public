"""F3 acceptance — upstream error mapping + sanitization (and the F1 error).

One case per branch (401, 403, generic 4xx, 5xx) asserting the user-facing MCP
error and the ``error_type``, plus sanitization tests proving stack traces /
internal hints never reach the client and that 5xx detail is kept for logging
but not surfaced.
"""

from autods_mcp_server.errors import (
    ERROR_FORBIDDEN,
    ERROR_RATE_LIMITED,
    ERROR_UNAUTHENTICATED,
    ERROR_UPSTREAM,
    ERROR_UPSTREAM_CLIENT,
    map_upstream_error,
    rate_limited_result,
)


def _text(result) -> str:
    return result.content[0].text


def test_401_maps_to_unauthenticated() -> None:
    mapped = map_upstream_error(401, {"message": "expired"})
    assert mapped.error_type == ERROR_UNAUTHENTICATED
    assert mapped.result.isError is True
    assert _text(mapped.result).startswith(f"{ERROR_UNAUTHENTICATED}: ")
    assert "re-authenticate" in _text(mapped.result).lower()
    assert mapped.log_full is None


def test_403_maps_to_forbidden() -> None:
    mapped = map_upstream_error(403, {"detail": "nope"})
    assert mapped.error_type == ERROR_FORBIDDEN
    assert "permission" in _text(mapped.result).lower()


def test_generic_4xx_surfaces_sanitized_detail() -> None:
    mapped = map_upstream_error(422, {"detail": "store_id is required"})
    assert mapped.error_type == ERROR_UPSTREAM_CLIENT
    text = _text(mapped.result)
    assert "422" in text
    # A clean, useful validation hint is forwarded.
    assert "store_id is required" in text


def test_4xx_drops_detail_that_looks_like_a_stack_trace() -> None:
    leaky = {"detail": 'Traceback (most recent call last):\n  File "/app/x.py", line 3'}
    mapped = map_upstream_error(400, leaky)
    text = _text(mapped.result)
    assert "Traceback" not in text
    assert "/app/" not in text
    # Falls back to the bare status message.
    assert "HTTP 400" in text


def test_4xx_detail_is_length_capped() -> None:
    mapped = map_upstream_error(400, {"detail": "x" * 500})
    # Capped (200) + ellipsis, plus the "HTTP 400." prefix — far below 500.
    assert len(_text(mapped.result)) < 300


def test_5xx_is_generic_to_user_but_keeps_detail_for_logging() -> None:
    mapped = map_upstream_error(503, {"detail": "db pool exhausted at pg-internal:5432"})
    text = _text(mapped.result)
    assert mapped.error_type == ERROR_UPSTREAM
    assert "503" in text
    # Internal detail must NOT reach the user...
    assert "pg-internal" not in text
    # ...but is preserved for the server-side log.
    assert mapped.log_full == {"detail": "db pool exhausted at pg-internal:5432"}


def test_3xx_is_generic_redirect_and_keeps_detail_for_logging() -> None:
    # follow_redirects=False → a 3xx is an upstream misconfiguration. The user
    # gets a generic redirect message; the Location/body (internal hosts) is
    # never surfaced but is kept for the server-side log.
    mapped = map_upstream_error(302, {"location": "http://internal-host/login"})
    text = _text(mapped.result)
    assert mapped.error_type == ERROR_UPSTREAM
    assert "redirect" in text.lower()
    assert "302" in text
    assert "internal-host" not in text
    assert mapped.log_full == {"location": "http://internal-host/login"}


def test_non_dict_4xx_body_is_handled() -> None:
    mapped = map_upstream_error(404, "plain not found text")
    assert mapped.error_type == ERROR_UPSTREAM_CLIENT
    assert "plain not found text" in _text(mapped.result)


def test_rate_limited_result_reports_retry_after() -> None:
    result = rate_limited_result(2.3)
    assert result.isError is True
    text = result.content[0].text
    assert text.startswith(f"{ERROR_RATE_LIMITED}: ")
    # Ceil'd to whole seconds.
    assert "3 seconds" in text


def test_rate_limited_result_floor_is_one_second() -> None:
    assert "1 second" in rate_limited_result(0.0).content[0].text
