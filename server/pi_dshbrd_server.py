#!/usr/bin/env python3
"""pi_dshbrd_server.py — tiny status HTTP endpoint for Raspberry Pi.

Reads a handful of cheap system facts and serves them as JSON on
:8081/temp so other devices on the LAN (e.g. an M5Stack Cardputer
running the ``pi_dashboard`` app) can poll them. Deliberately
dependency-free: it uses only the Python 3 standard library so it runs
on a stock Raspberry Pi OS image with no pip install step.

    GET /temp -> {
        "hostname": "raspberrypi",
        "temp_c": 47.3,
        "uptime": "6 days, 2 hours, 26 minutes",
        "updates": 3,
        "disk_free_gb": 41.7,
        "ram_used_mb": 1842,
        "ram_total_mb": 7936,
        "tailscale_connected": true,
        "tailscale_peers": 4,
        "docker_containers": [{"name": "redis", "status": "running",
                               "uptime": "Up 2 hours"}],
        "pihole_queries": 12043,
        "pihole_blocked": 1875,
        "pihole_blocked_pct": 15.6,
        "pihole_status": "enabled"
    }

Run it directly for a quick test:

    python3 pi_dshbrd_server.py

To start it automatically on boot, install it as the systemd service
described at the bottom of this file.
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Kernel exposes the SoC temperature here in millidegrees Celsius.
THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
# Seconds-since-boot lives in the first field of /proc/uptime.
UPTIME_PATH = "/proc/uptime"
PORT = 8081

# Resolve the hostname once at startup — it doesn't change while the
# process runs, and socket.gethostname() is cheap but there's no reason
# to call it on every request.
HOSTNAME = socket.gethostname()


def read_temp_c():
    """Return the CPU temperature in degrees Celsius as a float.

    The thermal_zone file holds an integer count of millidegrees (e.g.
    "47300\n" == 47.3 C), so we divide by 1000.
    """
    with open(THERMAL_PATH, "r") as f:
        millidegrees = int(f.read().strip())
    return millidegrees / 1000.0


def read_uptime():
    """Return system uptime as a human string, e.g.
    "6 days, 2 hours, 26 minutes".

    The first field of /proc/uptime is seconds-since-boot as a float.
    We break it into days / hours / minutes and pluralize each unit so
    the string reads naturally. Zero-valued leading units are dropped
    (a Pi up for 40 minutes reads "40 minutes", not
    "0 days, 0 hours, 40 minutes"); if everything rounds to zero we
    report "0 minutes".
    """
    with open(UPTIME_PATH, "r") as f:
        seconds = float(f.read().split()[0])

    total_minutes = int(seconds // 60)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)

    def _unit(n, name):
        return "{} {}{}".format(n, name, "" if n == 1 else "s")

    parts = []
    if days:
        parts.append(_unit(days, "day"))
    if hours or days:
        parts.append(_unit(hours, "hour"))
    parts.append(_unit(minutes, "minute"))
    return ", ".join(parts)


def _query_updates():
    """Run apt and return the available-update count as an int.

    Mirrors the shell one-liner
    ``apt list --upgradable 2>/dev/null | grep -c upgradable``: count
    the lines in apt's upgradable listing that mention "upgradable"
    (this skips apt's "Listing..." header). On any failure — apt
    missing, lock contention, timeout — we degrade to 0 rather than
    failing the whole response, since the temperature/uptime data is
    still useful.
    """
    try:
        out = subprocess.run(
            ["apt", "list", "--upgradable"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if "upgradable" in line)


# How long a cached apt count stays fresh. The dashboard polls every
# 5 s per client; running apt that often is wasteful and the upgradable
# set barely moves minute to minute, so we recompute at most once a
# minute and serve the cached value in between.
_UPDATES_TTL_S = 60

# Cache state, guarded by a lock because ThreadingHTTPServer dispatches
# each request on its own thread. ``_updates_at`` is the monotonic time
# of the last successful query; None means "never queried".
_updates_lock = threading.Lock()
_updates_value = 0
_updates_at = None


def count_updates():
    """Return the apt update count, recomputing at most once per minute.

    Within the TTL we return the cached value without touching apt. The
    apt call happens while holding the lock so a burst of concurrent
    requests on a cold/expired cache collapses into a single
    invocation rather than spawning N apt processes at once.
    """
    global _updates_value, _updates_at
    with _updates_lock:
        now = time.monotonic()
        if _updates_at is None or (now - _updates_at) >= _UPDATES_TTL_S:
            _updates_value = _query_updates()
            _updates_at = now
        return _updates_value


def disk_free_gb():
    """Return free space on / in gigabytes (base-1000) as a float.

    Uses os.statvfs: available blocks for unprivileged users
    (``f_bavail``) times the fragment size (``f_frsize``). Rounded to
    one decimal — sub-100 MB precision isn't meaningful on a dashboard.
    """
    st = os.statvfs("/")
    free_bytes = st.f_bavail * st.f_frsize
    return round(free_bytes / 1_000_000_000, 1)


# -----------------------------------------------------------------------------
# RAM (no cache — /proc/meminfo is a cheap in-memory read)
# -----------------------------------------------------------------------------


def read_ram_mb():
    """Return ``(used_mb, total_mb)`` as integers from /proc/meminfo.

    "Used" is the kernel's own accounting: MemTotal - MemAvailable, which
    treats reclaimable cache/buffers as free (the figure ``free -m`` shows
    in its "available" column). Values in /proc/meminfo are in kB; we
    divide by 1024 to MB.
    """
    total_kb = avail_kb = None
    with open("/proc/meminfo", "r") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
            if total_kb is not None and avail_kb is not None:
                break
    used_kb = total_kb - avail_kb
    return used_kb // 1024, total_kb // 1024


# -----------------------------------------------------------------------------
# Tailscale (cache 30 s)
# -----------------------------------------------------------------------------

_TAILSCALE_TTL_S = 30
_tailscale_lock = threading.Lock()
_tailscale_value = {"tailscale_connected": False, "tailscale_peers": 0}
_tailscale_at = None


def _query_tailscale():
    """Run ``tailscale status --json`` and summarize it.

    Returns ``{"tailscale_connected": bool, "tailscale_peers": int}``.
    Connected means the daemon's BackendState is "Running"; peers is the
    number of nodes in the tailnet visible to us. Any failure — binary
    missing, not logged in, 2 s timeout, bad JSON — degrades to
    disconnected / zero peers.
    """
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).stdout.decode("utf-8", "replace")
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"tailscale_connected": False, "tailscale_peers": 0}
    connected = data.get("BackendState") == "Running"
    peers = len(data.get("Peer") or {})
    return {"tailscale_connected": connected, "tailscale_peers": peers}


def tailscale_status():
    """Cached Tailscale summary, recomputed at most once per 30 s."""
    global _tailscale_value, _tailscale_at
    with _tailscale_lock:
        now = time.monotonic()
        if _tailscale_at is None or (now - _tailscale_at) >= _TAILSCALE_TTL_S:
            _tailscale_value = _query_tailscale()
            _tailscale_at = now
        return _tailscale_value


# -----------------------------------------------------------------------------
# Docker (cache 60 s, same TTL pattern as the apt count)
# -----------------------------------------------------------------------------

_DOCKER_TTL_S = 60
_docker_lock = threading.Lock()
_docker_value = []
_docker_at = None


def _shorten_container(name):
    """Strip the ``nextcloud-`` compose prefix and ``-1`` replica suffix
    so e.g. ``nextcloud-redis-1`` displays as ``redis`` on the tiny
    Cardputer cards."""
    if name.startswith("nextcloud-"):
        name = name[len("nextcloud-"):]
    if name.endswith("-1"):
        name = name[:-len("-1")]
    return name


def _query_docker():
    """Return a list of ``{name, status, uptime}`` for every container.

    Uses ``docker ps -a --format json`` (one JSON object per line). If
    Docker isn't installed, the daemon is unreachable, or the call times
    out, we return an empty list rather than failing the response.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except ValueError:
            continue
        containers.append({
            "name": _shorten_container(c.get("Names", "")),
            "status": c.get("State", ""),     # running / exited / ...
            "uptime": c.get("Status", ""),    # "Up 2 hours" / "Exited ..."
        })
    return containers


def docker_containers():
    """Cached container list, recomputed at most once per 60 s."""
    global _docker_value, _docker_at
    with _docker_lock:
        now = time.monotonic()
        if _docker_at is None or (now - _docker_at) >= _DOCKER_TTL_S:
            _docker_value = _query_docker()
            _docker_at = now
        return _docker_value


# -----------------------------------------------------------------------------
# Pi-hole (cache 60 s, cached session sid with re-auth on 401)
# -----------------------------------------------------------------------------

PIHOLE_SECRET_PATH = os.path.expanduser("~/.pihole_secret")
_PIHOLE_BASE = "http://127.0.0.1"
_PIHOLE_TTL_S = 60
_NULL_PIHOLE = {
    "pihole_queries": None,
    "pihole_blocked": None,
    "pihole_blocked_pct": None,
    "pihole_status": None,
}
_pihole_lock = threading.Lock()
_pihole_value = dict(_NULL_PIHOLE)
_pihole_at = None
_pihole_sid = None


def _pihole_read_secret():
    """Return the app password from ~/.pihole_secret, or None if the file
    doesn't exist (the case on NC-Pi5, which doesn't run Pi-hole)."""
    try:
        with open(PIHOLE_SECRET_PATH, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _pihole_auth(password):
    """POST the app password to /api/auth and return the session sid."""
    body = json.dumps({"password": password}).encode("utf-8")
    req = urllib.request.Request(
        _PIHOLE_BASE + "/api/auth",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return (data.get("session") or {}).get("sid")


def _pihole_get(path, sid):
    """Authenticated GET against the Pi-hole v6 API, sid in the header."""
    req = urllib.request.Request(
        _PIHOLE_BASE + path,
        headers={"X-FTL-SID": sid or ""},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _query_pihole(password):
    """Fetch the stats summary, reusing the cached sid and
    re-authenticating once if the session has expired (401)."""
    global _pihole_sid
    if _pihole_sid is None:
        _pihole_sid = _pihole_auth(password)
    try:
        summary = _pihole_get("/api/stats/summary", _pihole_sid)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        # Session expired — re-authenticate and retry once.
        _pihole_sid = _pihole_auth(password)
        summary = _pihole_get("/api/stats/summary", _pihole_sid)

    q = summary.get("queries") or {}
    status = (summary.get("gravity") or {}).get("status") \
        if isinstance(summary.get("gravity"), dict) else None
    if status not in ("enabled", "disabled"):
        # Blocking state isn't in the summary on every build; fall back to
        # the dedicated endpoint so the card can color the label.
        try:
            blk = _pihole_get("/api/dns/blocking", _pihole_sid)
            status = blk.get("blocking")
        except (urllib.error.URLError, OSError, ValueError):
            status = None
        if status not in ("enabled", "disabled"):
            status = None
    return {
        "pihole_queries": int(q.get("total") or 0),
        "pihole_blocked": int(q.get("blocked") or 0),
        "pihole_blocked_pct": round(float(q.get("percent_blocked") or 0.0), 1),
        "pihole_status": status,
    }


def pihole_stats():
    """Cached Pi-hole summary, recomputed at most once per 60 s.

    Returns all fields as None when the secret file is absent or any step
    fails, so a Pi without Pi-hole (or a transient API error) shows N/A on
    the dashboard rather than breaking the response.
    """
    global _pihole_value, _pihole_at
    with _pihole_lock:
        now = time.monotonic()
        if _pihole_at is not None and (now - _pihole_at) < _PIHOLE_TTL_S:
            return _pihole_value
        password = _pihole_read_secret()
        if password is None:
            _pihole_value = dict(_NULL_PIHOLE)
        else:
            try:
                _pihole_value = _query_pihole(password)
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                _pihole_value = dict(_NULL_PIHOLE)
        _pihole_at = now
        return _pihole_value


def build_payload():
    """Assemble the full status dict. May raise OSError/ValueError if a
    source file can't be read; the handler turns that into a 500."""
    ram_used_mb, ram_total_mb = read_ram_mb()
    ts = tailscale_status()
    payload = {
        "hostname": HOSTNAME,
        "temp_c": read_temp_c(),
        "uptime": read_uptime(),
        "updates": count_updates(),
        "disk_free_gb": disk_free_gb(),
        "ram_used_mb": ram_used_mb,
        "ram_total_mb": ram_total_mb,
        "tailscale_connected": ts["tailscale_connected"],
        "tailscale_peers": ts["tailscale_peers"],
        "docker_containers": docker_containers(),
    }
    payload.update(pihole_stats())
    return payload


class TempHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/temp":
            self.send_error(404, "Not Found")
            return

        try:
            body = json.dumps(build_payload()).encode("utf-8")
        except (OSError, ValueError) as e:
            # Couldn't read or parse the thermal file — surface it as a
            # 500 with a JSON error body so a polling client can log
            # something useful rather than a bare connection failure.
            body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Quiet the default per-request stderr logging; journald already
        # captures stdout/stderr and the access spam isn't useful here.
        pass


def main():
    # Bind on all interfaces so other hosts on the LAN can reach it.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TempHandler)
    print("pi_dshbrd_server listening on :{}/temp as {}".format(PORT, HOSTNAME))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


# -----------------------------------------------------------------------------
# Install as a systemd service (runs on boot)
# -----------------------------------------------------------------------------
#
# 1. Copy this script somewhere stable, e.g.:
#
#        sudo install -m 0755 pi_dshbrd_server.py /usr/local/bin/pi_dshbrd_server.py
#
# 2. Create the unit file at /etc/systemd/system/pi_dshbrd_server.service:
#
#        sudo tee /etc/systemd/system/pi_dshbrd_server.service >/dev/null <<'EOF'
#        [Unit]
#        Description=Raspberry Pi dashboard status HTTP server
#        After=network-online.target
#        Wants=network-online.target
#
#        [Service]
#        ExecStart=/usr/bin/python3 /usr/local/bin/pi_dshbrd_server.py
#        Restart=on-failure
#        RestartSec=5
#        # Runs as nobody: it only reads world-readable files (thermal
#        # zone, /proc/uptime, apt's cache) and shells out to `apt list`,
#        # none of which need privilege. ProtectSystem=strict keeps the
#        # filesystem read-only; apt list is read-only so it's happy.
#        User=nobody
#        Group=docker
#        NoNewPrivileges=true
#        ProtectSystem=strict
#        ProtectHome=true
#        PrivateTmp=true
#
#        [Install]
#        WantedBy=multi-user.target
#        EOF
#
# 3. Enable and start it:
#
#        sudo systemctl daemon-reload
#        sudo systemctl enable --now pi_dshbrd_server.service
#
# 4. Verify:
#
#        systemctl status pi_dshbrd_server.service
#        curl http://localhost:8081/temp
