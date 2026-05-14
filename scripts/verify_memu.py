#!/usr/bin/env python3
import os
import requests
import sys

# Agent memU health probe script
def verify_memu():
    url = "https://api-production-86f5.up.railway.app/api/v1/memu/health"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ memU is ONLINE (Production). Status: {data.get('db', 'unknown')}. Memories: {data.get('total_memories', 0)}")
            return True
        else:
            print(f"❌ memU returned {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ memU connection failed: {e}")
        return False

if __name__ == "__main__":
    success = verify_memu()
    sys.exit(0 if success else 1)
