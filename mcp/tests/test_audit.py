"""Tests for the consent audit log (audit.py) and its wiring into `confirm`.

The audit trail is a security feature: every `confirm` decision — approved,
denied, or timed out — must land on disk, and a broken log must never break a
confirm. These tests pin both the JSONL format and the fail-safe behavior.
"""

import json

import pytest

import server
from audit import ConsentAuditLog


def _read_lines(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ---- ConsentAuditLog unit behavior ---------------------------------


def test_disabled_when_path_is_none(tmp_path):
    log = ConsentAuditLog(None)
    assert log.enabled is False
    # No-op, and crucially does not raise.
    log.record(tool="confirm", agent="local", title="x", outcome="confirmed")


def test_records_one_jsonl_line_per_decision(tmp_path):
    p = tmp_path / "audit.log"
    log = ConsentAuditLog(p, clock=lambda: 1000.0)
    log.record(
        tool="confirm",
        agent="managed-agent",
        title="DROP customers",
        details="DELETE FROM customers;",
        outcome="confirmed",
        hold_ms=3120,
    )
    entries = _read_lines(p)
    assert entries == [
        {
            "ts": 1000.0,
            "tool": "confirm",
            "agent": "managed-agent",
            "title": "DROP customers",
            "outcome": "confirmed",
            "details": "DELETE FROM customers;",
            "hold_ms": 3120,
        }
    ]


def test_appends_across_calls(tmp_path):
    p = tmp_path / "audit.log"
    log = ConsentAuditLog(p, clock=lambda: 1.5)
    log.record(tool="confirm", agent="a", title="one", outcome="confirmed")
    log.record(tool="confirm", agent="b", title="two", outcome="cancelled")
    entries = _read_lines(p)
    assert [e["title"] for e in entries] == ["one", "two"]
    assert [e["outcome"] for e in entries] == ["confirmed", "cancelled"]


def test_optional_fields_omitted_when_none(tmp_path):
    p = tmp_path / "audit.log"
    log = ConsentAuditLog(p, clock=lambda: 0.0)
    log.record(tool="confirm", agent="local", title="deploy", outcome="timeout")
    (entry,) = _read_lines(p)
    assert "details" not in entry
    assert "hold_ms" not in entry


def test_hold_ms_coerced_to_int(tmp_path):
    p = tmp_path / "audit.log"
    log = ConsentAuditLog(p, clock=lambda: 0.0)
    log.record(
        tool="confirm", agent="local", title="x", outcome="confirmed", hold_ms=3009.7
    )
    (entry,) = _read_lines(p)
    assert entry["hold_ms"] == 3009


def test_creates_parent_directory(tmp_path):
    p = tmp_path / "nested" / "dir" / "audit.log"
    log = ConsentAuditLog(p, clock=lambda: 0.0)
    log.record(tool="confirm", agent="local", title="x", outcome="confirmed")
    assert p.exists()


def test_write_failure_is_swallowed_and_warned(tmp_path):
    # Point the log at a path whose parent is a *file*, so mkdir/open fails.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file")
    warnings = []
    log = ConsentAuditLog(
        blocker / "audit.log", clock=lambda: 0.0, warn=warnings.append
    )
    # Must not raise even though the write cannot succeed.
    log.record(tool="confirm", agent="local", title="x", outcome="confirmed")
    assert warnings and "audit log write failed" in warnings[0]


# ---- wiring into the confirm tool ----------------------------------


@pytest.fixture
def audit_to_tmp(tmp_path, monkeypatch):
    """Redirect the server's module-level audit log at a temp file."""
    p = tmp_path / "audit.log"
    monkeypatch.setattr(server, "_audit", ConsentAuditLog(p, clock=lambda: 42.0))
    return p


async def _fake_send(result):
    async def _send(cmd, payload, rpc_timeout_s=30, agent="mcp-client"):
        return result

    return _send


async def test_confirm_logs_confirmed(audit_to_tmp, monkeypatch):
    monkeypatch.setattr(
        server.bridge,
        "send",
        await _fake_send({"ack": "confirm", "ok": True, "confirmed": True, "hold_ms": 3210}),
    )
    ret = await server.confirm(None, "DROP customers", "DELETE FROM customers;", 30)
    assert ret == "confirmed (held 3210 ms)"
    (entry,) = _read_lines(audit_to_tmp)
    assert entry["outcome"] == "confirmed"
    assert entry["agent"] == "local"  # ctx=None resolves to the local label
    assert entry["title"] == "DROP customers"
    assert entry["details"] == "DELETE FROM customers;"
    assert entry["hold_ms"] == 3210


async def test_confirm_logs_cancelled(audit_to_tmp, monkeypatch):
    monkeypatch.setattr(
        server.bridge,
        "send",
        await _fake_send({"ack": "confirm", "ok": True, "cancelled": True}),
    )
    ret = await server.confirm(None, "force push", timeout_s=30)
    assert ret == "cancelled"
    (entry,) = _read_lines(audit_to_tmp)
    assert entry["outcome"] == "cancelled"
    # No details passed → field omitted.
    assert "details" not in entry


async def test_confirm_logs_unavailable(audit_to_tmp, monkeypatch):
    monkeypatch.setattr(
        server.bridge,
        "send",
        await _fake_send({"ack": "confirm", "ok": False, "err": "unavailable: device off"}),
    )
    ret = await server.confirm(None, "deploy prod", timeout_s=30)
    assert ret.startswith("unavailable")
    (entry,) = _read_lines(audit_to_tmp)
    assert entry["outcome"] == "unavailable"


async def test_confirm_input_error_is_not_logged(audit_to_tmp):
    # A rejected-before-send validation error never reached the device, so it
    # is not a decision and must not pollute the consent trail.
    ret = await server.confirm(None, "x", timeout_s=999)
    assert ret.startswith("error")
    assert not audit_to_tmp.exists()
