#!/usr/bin/env bash
# Generate a NATS NKey user keypair for the local fumemory NATS server.
#
# Output:
#   - Prints the public key (put this in nats-server.conf authorization.users[].nkey)
#   - Prints the seed    (put this in .env as NATS_LOCAL_NKEY_SEED)
#   - If --write is passed, patches nats-server.conf in-place and appends
#     NATS_LOCAL_NKEY_SEED to .env (or overwrites the placeholder line).
#
# Requirements: python3 with the nkeys package (pip install nkeys).
# Install once: pip install nkeys
#
# Usage:
#   ./gen-nkeys.sh              # print keys only
#   ./gen-nkeys.sh --write      # patch nats-server.conf and .env automatically

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="${SCRIPT_DIR}/nats-server.conf"
ENV_FILE="${SCRIPT_DIR}/../../.env"

# Generate the keypair via Python
KEYPAIR=$(python3 - <<'PYEOF'
import sys
try:
    import nkeys
    import os
    raw = os.urandom(32)
    seed = nkeys.encode_seed(raw, nkeys.PREFIX_BYTE_USER)
    kp = nkeys.from_seed(seed)
    print(kp.public_key.decode())
    print(seed.decode())
except ImportError:
    print("ERROR: nkeys Python package not found. Run: pip install nkeys", file=sys.stderr)
    sys.exit(1)
PYEOF
)

PUBLIC_KEY=$(echo "$KEYPAIR" | head -1)
SEED=$(echo "$KEYPAIR" | tail -1)

echo ""
echo "=== fumemory local NATS NKey keypair ==="
echo ""
echo "Public key (nats-server.conf):"
echo "  $PUBLIC_KEY"
echo ""
echo "Seed (.env as NATS_LOCAL_NKEY_SEED):"
echo "  $SEED"
echo ""

if [[ "${1:-}" == "--write" ]]; then
    # Patch the placeholder in nats-server.conf
    if [[ -f "$CONF_FILE" ]]; then
        # Replace the placeholder 'U' + 56 As with the real public key
        sed -i "s|{ nkey: \"U[A-Z2-7]*\" }|{ nkey: \"${PUBLIC_KEY}\" }|" "$CONF_FILE"
        echo "✓ Patched ${CONF_FILE} with public key."
    else
        echo "WARNING: ${CONF_FILE} not found — update manually."
    fi

    # Append or replace NATS_LOCAL_NKEY_SEED in .env
    if [[ -f "$ENV_FILE" ]]; then
        if grep -q "^NATS_LOCAL_NKEY_SEED=" "$ENV_FILE"; then
            sed -i "s|^NATS_LOCAL_NKEY_SEED=.*|NATS_LOCAL_NKEY_SEED=${SEED}|" "$ENV_FILE"
            echo "✓ Updated NATS_LOCAL_NKEY_SEED in ${ENV_FILE}."
        else
            echo "NATS_LOCAL_NKEY_SEED=${SEED}" >> "$ENV_FILE"
            echo "✓ Appended NATS_LOCAL_NKEY_SEED to ${ENV_FILE}."
        fi
    else
        echo "WARNING: ${ENV_FILE} not found — add manually:"
        echo "  NATS_LOCAL_NKEY_SEED=${SEED}"
    fi

    echo ""
    echo "Restart the local NATS server to apply the new keypair."
else
    echo "Run with --write to patch nats-server.conf and .env automatically."
fi
