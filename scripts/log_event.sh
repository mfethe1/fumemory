#!/usr/bin/env bash
set -euo pipefail
EVENT="${1:-event}"
LEVEL="${2:-info}"
PAYLOAD="${3:-{}}"
python3 - <<'PY'
import json, os
from memu.structured_logging import write_event
write_event(os.environ.get('EVENT','event'), os.environ.get('LEVEL','info'), json.loads(os.environ.get('PAYLOAD','{}')))
print('{"ok":true}')
PY
