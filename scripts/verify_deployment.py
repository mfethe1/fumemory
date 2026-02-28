import time
import requests
import os
import sys
import json

# Configuration
API_URL = "https://fumemory-infra-production-b652.up.railway.app"
API_KEY = os.environ.get("MEMU_API_KEY")

if not API_KEY:
    # Try to load from secrets file
    try:
        with open(os.path.expanduser("~/.openclaw/secrets/memu_api_key"), "r") as f:
            API_KEY = f.read().strip()
    except:
        print("❌ MEMU_API_KEY not found in env or secrets.")
        sys.exit(1)

HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

def check_health():
    print(f"🏥 Checking health at {API_URL}/health...")
    try:
        resp = requests.get(f"{API_URL}/health", timeout=10)
        if resp.status_code == 200:
            print(f"✅ Healthy: {resp.json()}")
            return True
        else:
            print(f"❌ Unhealthy: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

def test_async_ingest():
    print("\n🚀 Testing Async Memory Ingestion...")
    payload = {
        "content": f"Deployment Verification Test {time.time()}",
        "agent_id": "macklemore-qa",
        "memory_type": "observation",
        "metadata": {"source": "deployment_test", "verified": True}
    }
    
    try:
        # Hit the new async endpoint
        resp = requests.post(f"{API_URL}/memories/async", headers=HEADERS, json=payload, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            wf_id = data.get("workflow_id")
            print(f"✅ Ingestion Accepted. Workflow ID: {wf_id}")
            return payload["content"]
        else:
            print(f"❌ Ingestion Failed: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return None

def verify_ingestion(content):
    print("\n🔍 Verifying Ingestion (Waiting for Workflow)...")
    
    # Poll for up to 30 seconds
    for i in range(6):
        time.sleep(5)
        print(f"   Polling attempt {i+1}...")
        
        try:
            # Use text search to verify persistence
            resp = requests.post(
                f"{API_URL}/api/v1/memu/search-text", 
                headers=HEADERS, 
                json={"query": content, "limit": 1}
            )
            
            if resp.status_code == 200:
                results = resp.json()
                # Check for list or dict response format depending on API version
                # The search-text endpoint usually returns a list
                if isinstance(results, dict) and "results" in results:
                    results = results["results"]
                
                if results and len(results) > 0:
                    found_content = results[0].get("content", "")
                    if content in found_content:
                        print(f"✅ Verified! Found memory: {found_content[:50]}...")
                        return True
            else:
                print(f"   Search error: {resp.status_code}")
                
        except Exception as e:
            print(f"   Polling error: {e}")
            
    print("❌ Verification Timed Out. Memory not found after 30s.")
    return False

def test_async_search():
    print("\n🔎 Testing Async Search (Audit Log Generation)...")
    payload = {
        "query": "audit check",
        "agent_id": "macklemore-qa",
        "limit": 1
    }
    
    try:
        resp = requests.post(f"{API_URL}/search/async", headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ Async Search Accepted.")
            return True
        else:
            print(f"❌ Async Search Failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return False

def main():
    if not check_health():
        sys.exit(1)
        
    content = test_async_ingest()
    if content:
        if verify_ingestion(content):
            print("\n✅ INGESTION WORKFLOW PASS")
        else:
            print("\n❌ INGESTION WORKFLOW FAIL")
            sys.exit(1)
            
    if test_async_search():
        print("\n✅ SEARCH WORKFLOW PASS")
    else:
        print("\n❌ SEARCH WORKFLOW FAIL")
        sys.exit(1)

    print("\n🎉 Deployment Verification Complete: SUCCESS")

if __name__ == "__main__":
    main()
