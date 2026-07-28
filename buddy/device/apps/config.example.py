"""Push-to-Claude device config — copy to ``config.py`` and fill in.

The Push-to-Claude app loads ``WORKER_BASE`` and ``DEVICE_SECRET``
from this module at import time. ``config.py`` is gitignored so your
secret never leaves the device. See ``worker/README.md`` for how to
deploy your own Cloudflare Worker relay and where to get these
values.
"""

# Base URL of YOUR deployed Cloudflare Worker, e.g.
#   "https://push-to-claude.<your-subdomain>.workers.dev"
# No trailing slash; the app appends "/ask", "/ask-text", "/reset".
WORKER_BASE = ""

# Shared secret between this device and the Worker. Must match the
# DEVICE_SECRET you set on the Worker via:
#   wrangler secret put DEVICE_SECRET
# Generate one with: ``openssl rand -base64 32`` (or any random 32+ char string).
DEVICE_SECRET = ""

# Pi Dashboard endpoints — left card, right card.
# Format: "http://<ip>:<port>/temp"
PI_ENDPOINTS = (
    "",
    "",
)

# Scanner app — base URL of scanservjs (the SANE web frontend) on the Pi
# that has the scanner attached. LAN IP, no trailing slash, e.g.
#   "http://<pi-lan-ip>:8090"
# The Cardputer reaches it over the local network (not Tailscale), so use
# the Pi's LAN IP — ideally a DHCP reservation so it never changes.
SCANNER_BASE = ""

# Claude verbruik app — endpoint serving your Claude limit percentages,
# e.g. "http://<pi-lan-ip>:8091/verbruik". Run
# ``pi/verbruik/claude_verbruik_server.py`` on the machine that holds the
# Claude Code login; the device never sees a token, only percentages.
# For an offline dry-run, point this at ``fake_verbruik_server.py``.
VERBRUIK_ENDPOINT = ""