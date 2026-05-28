"""Regression tests for server._allowed_hosts.

The MCP transport's Host matcher does exact-match or `host:*` port
wildcards only — NOT `*.domain` prefix wildcards. The tunnel's mcp-proxy
forwards `Host: cardputer.<tunnel_domain>`, so that exact host MUST be in
the list or every tunneled request 421s. These pin that.
"""

import server


def test_loopback_hosts_always_present():
    hosts = server._allowed_hosts("127.0.0.1", 9000)
    assert "127.0.0.1:9000" in hosts
    assert "localhost:9000" in hosts


def test_tunnel_subdomain_host_is_allowed():
    dom = "abcd1234.tunnel.anthropic.com"
    hosts = server._allowed_hosts("127.0.0.1", 9000, tunnel_domain=dom)
    # This is the Host the daemon actually receives through the tunnel.
    assert f"cardputer.{dom}" in hosts
    assert dom in hosts


def test_no_dead_star_wildcard_entry():
    # The old bug: a "*.<domain>" entry that the matcher silently ignores.
    dom = "abcd1234.tunnel.anthropic.com"
    hosts = server._allowed_hosts("127.0.0.1", 9000, tunnel_domain=dom)
    assert f"*.{dom}" not in hosts


def test_docker_host_gateway_tolerated():
    # In case the proxy rewrites Host to the upstream target.
    hosts = server._allowed_hosts("127.0.0.1", 9000)
    assert "host.docker.internal:9000" in hosts
    assert "host.docker.internal" in hosts


def test_env_escape_hatch(monkeypatch):
    monkeypatch.setenv("CARDPUTER_ALLOWED_HOSTS", "foo.example:8080, bar.example ")
    hosts = server._allowed_hosts("127.0.0.1", 9000)
    assert "foo.example:8080" in hosts
    assert "bar.example" in hosts


def test_extra_param_appended():
    hosts = server._allowed_hosts("127.0.0.1", 9000, extra=["testserver"])
    assert "testserver" in hosts
