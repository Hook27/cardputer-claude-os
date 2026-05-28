#!/usr/bin/env bash
# Generate the CA + inner-TLS server certificate for the MCP tunnel.
#
# The tunnel's inner TLS is terminated by mcp-proxy using a cert signed by
# a CA YOU control and register in the Console — that's what stops the
# transport provider (Cloudflare) from reading or forging payloads. This
# is the "manual" setup flow from the MCP-tunnels docs.
#
# Outputs into ./data (gitignored):
#   ca.crt   -> upload this to your tunnel in the Console (Manage > MCP tunnels)
#   ca.key   -> keep secret; only used to sign the server cert
#   tls.crt  -> server cert, mounted into mcp-proxy
#   tls.key  -> server key, mounted into mcp-proxy (chmod 644 for UID 65532)
#
# Usage:  cd tunnel && cp env.example .env && $EDITOR .env && ./gen-certs.sh
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

: "${TUNNEL_DOMAIN:?Set TUNNEL_DOMAIN in tunnel/.env (copy env.example) first}"

mkdir -p data

echo "Generating CA..."
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout data/ca.key -out data/ca.crt \
  -days 3650 -subj "/CN=cardputer-mcp-tunnel-ca" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash"

cat > data/tls.ext <<EOF
subjectAltName = DNS:${TUNNEL_DOMAIN},DNS:*.${TUNNEL_DOMAIN}
authorityKeyIdentifier = keyid,issuer
extendedKeyUsage = serverAuth
EOF

echo "Generating server cert for ${TUNNEL_DOMAIN} (90 days)..."
openssl req -newkey rsa:2048 -nodes \
  -keyout data/tls.key -out /tmp/cardputer-tunnel-server.csr \
  -subj "/CN=${TUNNEL_DOMAIN}"
openssl x509 -req -in /tmp/cardputer-tunnel-server.csr \
  -CA data/ca.crt -CAkey data/ca.key -CAcreateserial \
  -out data/tls.crt -days 90 -extfile data/tls.ext
rm -f /tmp/cardputer-tunnel-server.csr

# Keep private keys owner-only — never world-readable. On Docker Desktop
# (macOS, the target host) the file-sharing layer still lets the proxy
# container (UID 65532) read the bind-mounted tls.key at 0600, so this is
# both secure and functional here.
#
# On NATIVE Linux Docker a 0600 host-owned key is NOT readable by UID
# 65532 in the container — if the proxy logs "permission denied", prefer a
# Docker named volume, or fall back to `chmod 640 data/tls.key` with a
# group the container UID shares. Do not use 0644.
chmod 600 data/ca.key data/tls.key

echo
echo "Done. Next:"
echo "  1. Upload tunnel/data/ca.crt in the Console (Manage > MCP tunnels > your tunnel)."
echo "  2. docker compose up -d"
echo
echo "Server cert expires in 90 days — re-run this script and re-upload"
echo "the CA (if regenerated) before then. Reusing the same ca.crt only"
echo "needs the tls.crt/tls.key regenerated; the proxy hot-reloads them."
