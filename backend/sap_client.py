import os
import json
import httpx
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("sap_client")

_http_client = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        base_url = os.getenv("VZONE_API_URL", "http://vzone.in:1662")
        _http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
        )
    return _http_client

def _write_audit(audit_record: dict):
    """Synchronous file write designed to be run in a thread executor."""
    try:
        with open("sap_audit_trail.log", "a") as f:
            f.write(json.dumps(audit_record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write to audit log: {e}")

async def log_audit_trail(query: str, status: str, details: str = "", company_id: str = "DEFAULT"):
    audit_record = {
        "timestamp": datetime.utcnow().isoformat(),
        "company_id": company_id,
        "query": query,
        "status": status,
        "details": details
    }
    # Write to file without blocking the async event loop
    await asyncio.to_thread(_write_audit, audit_record)

async def execute_sap_query(sql_query: str, company_id: str = "DEFAULT") -> dict:
    client = await get_http_client()
    minified_sql = " ".join(sql_query.split())
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    try:
        # Note: If VZone API allows POST for SQL execution, swap .get() to .post() for better security
        params = {"query": minified_sql, "company_id": company_id}
        response = await client.get("/api/GetMethod/GetData", params=params, headers=headers)
        
        if response.status_code == 200:
            await log_audit_trail(minified_sql, "SUCCESS", company_id=company_id)
            try:
                return response.json()
            except json.JSONDecodeError:
                error_msg = "API returned a non-JSON response."
                await log_audit_trail(minified_sql, "API_ERROR", error_msg, company_id)
                return {"error": error_msg}
                
        error_msg = f"VZone API Error: {response.status_code} - {response.text}"
        await log_audit_trail(minified_sql, "API_ERROR", error_msg, company_id)
        return {"error": error_msg}
        
    except Exception as e:
        error_msg = f"Connection failed: {str(e)}"
        await log_audit_trail(minified_sql, "EXCEPTION", error_msg, company_id)
        return {"error": error_msg}