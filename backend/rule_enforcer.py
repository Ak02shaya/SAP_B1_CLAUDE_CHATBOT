import re
import json
from pathlib import Path
from typing import List, Optional

_LITERAL = re.compile(r"'(?:[^']|'')*'")

_FLAG_COLUMNS = {
    "DOCTYPE", "CANCELED", "DOCSTATUS", "LINESTATUS", "TREETYPE",
    "INVNTITEM", "WHSCODE", "ITEMCODE", "CARDCODE", "ITMSGRPCOD",
}

_BLOCKED = [
    r"\bDROP\s+(TABLE|VIEW|DATABASE|SCHEMA|INDEX|USER)\b",
    r"\bDELETE\s+FROM\b", r"\bUPDATE\s+\S+\s+SET\b", r"\bINSERT\s+INTO\b",
    r"\bALTER\s+(TABLE|VIEW|DATABASE|SCHEMA|USER)\b", r"\bEXEC(UTE)?\s+",
    r"\bTRUNCATE\b", r"\bMERGE\s+INTO\b", r"\bGRANT\s+", r"\bREVOKE\s+",
    r"\bCREATE\s+(TABLE|VIEW|DATABASE|SCHEMA|INDEX|USER)\b", r"\bCALL\s+",
    r"--", r"/\*", r";", r"\bWITH\b",
]

# Load Tenant Config
CONFIG_PATH = Path(__file__).parent / "tenant_config.json"
with open(CONFIG_PATH, "r") as f:
    TENANT_CONFIG = json.load(f)

def _normalise(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())

def is_sql_safe(sql: Optional[str]) -> bool:
    if not sql or not isinstance(sql, str):
        return False
    code = _LITERAL.sub("''", _normalise(sql))
    if not code.upper().startswith("SELECT"):
        return False
    return not any(re.search(p, code, re.IGNORECASE) for p in _BLOCKED)

def check_business_rules(sql: str, company_id: str) -> List[str]:
    v: List[str] = []
    s = _normalise(sql)
    full = s.upper()                       
    code = _LITERAL.sub("''", s).upper()   

    # --- 1. UNIVERSAL RULES (Applies to ALL SAP B1 Companies) ---
    if not re.match(r"^SELECT\s+(DISTINCT\s+)?TOP\s+25\b", code):
        v.append("Query must start exactly with SELECT TOP 25.")

    if re.search(r"\b(ROUND|CAST)\s*\(", code):
        v.append("ROUND()/CAST() are forbidden; return raw numeric database values.")

    has_agg = re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", code)
    if ("GROUP BY" in code or not has_agg) and not re.search(r"\bORDER\s+BY\b", code):
        v.append("You must include an explicit ORDER BY clause for determinism.")

    for m in re.finditer(r"\b[A-Z0-9_]+\.\"(\w+)\"\s*(?:=|<>|NOT\s+LIKE|LIKE)\s*'", full):
        if m.group(1).upper() not in _FLAG_COLUMNS:
            v.append(f'Text filter on "{m.group(1)}" must use LOWER() on both sides.')
            break

    if re.search(r"\bOINV\b", code):
        if not re.search(r"\bOINV\s+(AS\s+)?T0\b", code):
            v.append("You must alias OINV as T0.")
        if not re.search(r"\bINV1\s+(AS\s+)?T1\b", code):
            v.append("You must join INV1 as T1.")
        if not re.search(r"T0\.\"DOCTYPE\"\s*=\s*'I'", full):
            v.append("Missing filter T0.\"DocType\" = 'I'.")
        if not re.search(r"T0\.\"CANCELED\"\s*=\s*'N'", full):
            v.append("Missing filter T0.\"CANCELED\" = 'N'.")

        if "U_REQWHS" in code:
            v.append("RULE VIOLATION: Never use U_ReqWhs. You MUST join via line-level T1.\"WhsCode\".")
            
        if not re.search(r"T1\.[\"']?WHSCODE[\"']?\s*=\s*OWHS\.[\"']?WHSCODE[\"']?", code) and \
           not re.search(r"OWHS\.[\"']?WHSCODE[\"']?\s*=\s*T1\.[\"']?WHSCODE[\"']?", code):
            v.append('RULE VIOLATION: You must join warehouses exactly via T1."WhsCode" = OWHS."WhsCode".')
            
        if not re.search(r"T1\.\"TREETYPE\"\s*<>\s*'S'", full):
            v.append("Must exclude bundles. Ensure T1.\"TreeType\" <> 'S'.")

    # --- 2. TENANT-SPECIFIC RULES (From JSON Config) ---
    tenant_rules = TENANT_CONFIG.get(company_id, TENANT_CONFIG["DEFAULT"])
    custom_firewalls = tenant_rules.get("firewall_regex_rules", [])
    
    for regex_rule in custom_firewalls:
        # If the query hits OINV but is missing the custom exclusion, block it
        if re.search(r"\bOINV\b", code) and not re.search(regex_rule, full):
            v.append(f"Missing required business filter for this company. Rule regex: {regex_rule}")

    return v

def slice_rows(result, n: int = 25):
    return result[:n] if isinstance(result, list) else []