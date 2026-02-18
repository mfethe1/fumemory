#!/bin/bash
# memU CLI — Simple command-line client for memU
# Usage:
#   memu-cli health
#   memu-cli store <agent_id> <key> <content> [namespace]
#   memu-cli list [agent_id] [limit]
#   memu-cli search <query> [agent_id] [limit]
#   memu-cli search-text <query> [agent_id] [limit]
#   memu-cli get <memory_id>
#   memu-cli delete <memory_id>
#
# Env: MEMU_API_KEY (required), MEMU_BASE_URL (default: http://localhost:8000)

BASE_URL="${MEMU_BASE_URL:-http://localhost:8000}"
API_KEY="${MEMU_API_KEY:?Set MEMU_API_KEY}"

case "$1" in
  health)
    curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/health"
    ;;
  store)
    agent_id="${2:?agent_id required}"
    key="${3:?key required}"
    content="${4:?content required}"
    curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
      -d "$(python3 -c "import json,sys; print(json.dumps({'content':sys.argv[1],'agent_id':'$agent_id','metadata':{'key':'$key'}}))" "$content")" \
      "$BASE_URL/memories"
    ;;
  list)
    agent_id="${2:-}"
    limit="${3:-50}"
    params="limit=$limit"
    [ -n "$agent_id" ] && params="$params&agent_id=$agent_id"
    curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/memories?$params"
    ;;
  search)
    query="${2:?query required}"
    agent_id="${3:-}"
    limit="${4:-10}"
    payload="{\"query\":\"$query\",\"limit\":$limit"
    [ -n "$agent_id" ] && payload="$payload,\"agent_id\":\"$agent_id\""
    payload="$payload}"
    curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
      -d "$payload" "$BASE_URL/search"
    ;;
  search-text)
    query="${2:?query required}"
    agent_id="${3:-}"
    limit="${4:-10}"
    params="query=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$query'))")&limit=$limit"
    [ -n "$agent_id" ] && params="$params&agent_id=$agent_id"
    curl -s -X POST -H "X-API-Key: $API_KEY" "$BASE_URL/search-text?$params"
    ;;
  get)
    id="${2:?memory_id required}"
    curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/memories/$id"
    ;;
  delete)
    id="${2:?memory_id required}"
    curl -s -X DELETE -H "X-API-Key: $API_KEY" "$BASE_URL/memories/$id"
    ;;
  *)
    echo "Usage: $0 {health|store|list|search|search-text|get|delete}"
    exit 1
    ;;
esac
