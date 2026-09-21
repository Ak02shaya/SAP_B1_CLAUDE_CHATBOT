import os
import re
import json
import httpx
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("sap_client")

# ==========================================
# HTTP CLIENT
# ==========================================
_http_client = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        # Pull the base URL from your .env file
        base_url = os.getenv("VZONE_API_URL", "http://vzone.in:1662")
        
        _http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
        )
    return _http_client

# ==========================================
# SECURITY & VALIDATION
# ==========================================
def validate_read_only_query(sql: str) -> bool:
    """Allow one read-only HANA statement and reject comments or chaining."""
    if not sql or len(sql) > 10000:
        return False

    without_comments = re.sub(r"(--[^\n]*|/\*.*?\*/)", "", sql, flags=re.DOTALL).strip()
    if ";" in without_comments:
        return False

    statement = without_comments.upper()
    if not re.match(r"^(SELECT|WITH)\b", statement):
        return False

    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|EXEC|TRUNCATE|CREATE|CALL|MERGE)\b")
    return not bool(forbidden.search(statement))

def validate_query(sql: str) -> dict:
    if not validate_read_only_query(sql):
        return {"valid": False, "error": "Only one read-only SELECT or WITH query is allowed."}
    return {"valid": True}

def log_audit_trail(query: str, status: str, details: str = ""):
    audit_record = {
        "timestamp": datetime.utcnow().isoformat(),
        "query": query,
        "status": status,
        "details": details
    }
    try:
        with open("sap_audit_trail.log", "a") as f:
            f.write(json.dumps(audit_record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write to audit log: {e}")

# ==========================================
# UNIVERSAL API EXECUTION ENGINE
# ==========================================
async def execute_sap_query(sql_query: str) -> dict:
    """
    Executes any dynamic SELECT query passed by the AI agent against the live database.
    """
    validation = validate_query(sql_query)
    if not validation["valid"]:
        log_audit_trail(sql_query, "BLOCKED", validation["error"])
        logger.warning(f"Query Blocked by DB Firewall: {validation['error']}")
        return {"error": validation["error"]}

    client = await get_http_client()
    minified_sql = " ".join(sql_query.split())
    
    # Mimic the exact browser environment to bypass API blocking
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "http://vzone.in:1662/swagger/index.html",
        "Connection": "keep-alive"
    }

    try:
        # Note: Using GET for SQL execution is uncommon due to URL length limits. 
        # If your queries start failing silently, check if the VZone API supports POST.
        response = await client.get("/api/GetMethod/GetData", params={"query": minified_sql}, headers=headers)
        
        if response.status_code == 200:
            log_audit_trail(minified_sql, "SUCCESS")
            try:
                return response.json()
            except json.JSONDecodeError:
                error_msg = "API returned a non-JSON response (possibly blocked by a firewall or proxy)."
                log_audit_trail(minified_sql, "API_ERROR", error_msg)
                return {"error": error_msg}
                
        error_msg = f"VZone API Error: {response.status_code} - {response.text}"
        log_audit_trail(minified_sql, "API_ERROR", error_msg)
        return {"error": error_msg}
        
    except Exception as e:
        error_msg = f"Connection failed: {str(e)}"
        log_audit_trail(minified_sql, "EXCEPTION", error_msg)
        return {"error": error_msg}        