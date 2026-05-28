"""Unit tests for the bearer-auth module (mcp/auth.py).

These are pure-Python and need no BLE device or running server.
"""

from auth import label_for_authorization, parse_token_map


def test_parse_token_map_basic():
    assert parse_token_map("a=claude-code,b=managed-agent") == {
        "a": "claude-code",
        "b": "managed-agent",
    }


def test_parse_token_map_empty():
    assert parse_token_map("") == {}
    assert parse_token_map(None) == {}


def test_parse_token_map_trims_whitespace():
    assert parse_token_map(" a = local , b = cloud ") == {"a": "local", "b": "cloud"}


def test_parse_token_map_ignores_malformed_pairs():
    # A pair with no '=' is skipped rather than crashing the daemon.
    assert parse_token_map("a=local,garbage,b=cloud") == {"a": "local", "b": "cloud"}


def test_parse_token_map_skips_blank_token():
    assert parse_token_map("=label,a=local") == {"a": "local"}


def test_label_for_authorization_valid():
    tm = {"sek": "claude-code"}
    assert label_for_authorization("Bearer sek", tm) == "claude-code"


def test_label_for_authorization_case_insensitive_scheme():
    tm = {"sek": "claude-code"}
    assert label_for_authorization("bearer sek", tm) == "claude-code"


def test_label_for_authorization_missing_or_bad():
    tm = {"sek": "claude-code"}
    assert label_for_authorization(None, tm) is None
    assert label_for_authorization("Bearer nope", tm) is None
    assert label_for_authorization("sek", tm) is None  # no Bearer prefix
    assert label_for_authorization("Bearer ", tm) is None


def test_label_for_authorization_empty_map_denies_all():
    assert label_for_authorization("Bearer anything", {}) is None
