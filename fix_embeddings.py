import re

def fix_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Fix the parsing for both /v1/embeddings (data[0].embedding) and /api/embeddings (embedding directly)
    new_try_body = """            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    r = await client.post(url, headers=headers, json=body)
                    r.raise_for_status()
                    res = r.json()
                    if "data" in res and res["data"]:
                        emb = res["data"][0].get("embedding")
                        if emb is not None:
                            return emb
                    if "embedding" in res:
                        return res["embedding"]
            except Exception as e:
                logger.warning("Embedding probe failed for %s: %s", url, e)
            return None"""
            
    # Quick replace in memu/api.py
    # We will use python regex to replace the function body
    pass

