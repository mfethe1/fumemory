import time
import requests
import os
import sys

API_URL = os.environ.get(
    "MEMU_API_URL",
    "https://api-production-86f5.up.railway.app/api/v1/memu",
).rstrip("/")
API_KEY = os.environ.get("MEMU_API_KEY")

if not API_KEY:
    try:
        with open(os.path.expanduser("~/.openclaw/secrets/memu_api_key"), "r") as f:
            API_KEY = f.read().strip()
    except Exception:
        print("❌ MEMU_API_KEY not found in env or secrets.")
        sys.exit(1)

HEADERS = {
    "X-MemU-Key": API_KEY,
    "Content-Type": "application/json",
}


def check_health():
    print(f"🏥 Checking health at {API_URL}/health...")
    try:
        resp = requests.get(f"{API_URL}/health", headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            print(f"✅ Healthy: {resp.json()}")
            return True
        print(f"❌ Unhealthy: {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


def test_ingest():
    print("\n🚀 Testing Memory Ingestion...")
    payload = {
        "content": f"Deployment Verification Test {time.time()}",
        "agent_id": "macklemore-qa",
        "memory_type": "fact",
        "metadata": {"source": "deployment_test", "verified": True},
    }

    try:
        resp = requests.post(f"{API_URL}/add", headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Ingestion Accepted. Memory ID: {data.get('id')}")
            return payload["content"]
        print(f"❌ Ingestion Failed: {resp.status_code} - {resp.text}")
        return None
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return None


def verify_ingestion(content):
    print("\n🔍 Verifying Ingestion...")
    for i in range(6):
        time.sleep(2)
        print(f"   Polling attempt {i + 1}...")
        try:
            resp = requests.post(
                f"{API_URL}/search-text",
                headers=HEADERS,
                json={"query": content, "limit": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json()
                if isinstance(results, dict):
                    results = results.get("results") or results.get("memories") or []
                if results and content in results[0].get("content", ""):
                    print(f"✅ Verified! Found memory: {results[0].get('content', '')[:50]}...")
                    return True
            else:
                print(f"   Search error: {resp.status_code}")
        except Exception as e:
            print(f"   Polling error: {e}")
    print("❌ Verification Timed Out. Memory not found.")
    return False


def test_search():
    print("\n🔎 Testing Search...")
    payload = {"query": "deployment test", "agent_id": "macklemore-qa", "limit": 1}
    try:
        resp = requests.post(f"{API_URL}/search-text", headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ Search Accepted.")
            return True
        print(f"❌ Search Failed: {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return False


def main():
    if not check_health():
        sys.exit(1)

    content = test_ingest()
    if not content or not verify_ingestion(content):
        print("\n❌ INGESTION WORKFLOW FAIL")
        sys.exit(1)

    print("\n✅ INGESTION WORKFLOW PASS")

    if test_search():
        print("\n✅ SEARCH WORKFLOW PASS")
    else:
        print("\n❌ SEARCH WORKFLOW FAIL")
        sys.exit(1)

    print("\n🎉 Deployment Verification Complete: SUCCESS")


if __name__ == "__main__":
    main()
