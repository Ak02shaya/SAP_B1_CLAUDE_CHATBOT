import os
import json
import re
import time
import asyncio
import logging
from typing import Optional

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from backend.sap_client import execute_sap_query

load_dotenv()
logger = logging.getLogger("sap_ai_assistant")
logging.basicConfig(level=logging.INFO)

client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- GLOBAL SCHEMA CACHE ---
SCHEMA_CACHE = {}

# --- 1. RESPONSE MODEL ---
class DashboardResponse(BaseModel):
    analysis: str = Field(description="Structured dashboard report formatted strictly with Markdown tables and visual sections.")
    solution_evaluation: str = Field(description="Key observations and analytical takeaway without table names.")
    suggestions: list[str] = Field(description="3 context-aware follow-up business queries.")

dashboard_schema = DashboardResponse.model_json_schema()
dashboard_schema.pop("title", None)

def _empty_dashboard(analysis: str, evaluation: str, suggestions: list[str], exec_ms: float = 0.0) -> dict:
    return {
        "analysis": analysis,
        "solution_evaluation": evaluation,
        "suggestions": suggestions,
        "execution_time_ms": exec_ms,
    }

# --- 2. SQL SAFETY GUARDRAIL ---
def is_sql_safe(query: Optional[str]) -> bool:
    if not query or not isinstance(query, str):
        return False
    clean_sql = query.strip()
    if clean_sql.endswith(";"):
        clean_sql = clean_sql[:-1].strip()
    if not clean_sql.upper().startswith("SELECT"):
        return False

    blocked_patterns = [
        r"\bDROP\b", r"\bDELETE\b", r"\bUPDATE\b", r"\bINSERT\b",
        r"\bALTER\b", r"\bEXEC\b", r"\bEXECUTE\b", r"\bTRUNCATE\b",
        r"\bMERGE\b", r"\bCALL\b", r"\bGRANT\b", r"\bREVOKE\b",
        r"\bCREATE\b", r"\bRENAME\b", r"\bBACKUP\b", r"\bSYS\.",
        r"--", r";", r"\bWITH\b"
    ]
    return not any(re.search(pattern, clean_sql, re.IGNORECASE) for pattern in blocked_patterns)

# --- 3. AUTONOMOUS METADATA TOOLS ---
TOOLS = [
    {
        "name": "discover_metadata",
        "description": "Queries SAP B1 CUFD to discover custom fields (U_). Use this FIRST if a user asks for a custom dimension.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "The current tenant ID."},
                "table_id": {"type": "string", "description": "SAP table to inspect (e.g., OINV, OITM)."},
                "search_keyword": {"type": "string", "description": "Optional keyword like 'branch' or 'brand'."}
            },
            "required": ["company_id", "table_id"]
        }
    },
    {
        "name": "execute_sap_sql",
        "description": "Executes a single read-only SELECT statement on SAP HANA. Use this to fetch the final data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql_query": {"type": "string", "description": "Flat HANA SELECT query. NO WITH clauses, NO subqueries."}
            },
            "required": ["sql_query"]
        }
    },
    {
        "name": "submit_dashboard",
        "description": "Use this tool ONLY when you have successfully executed the SQL and have the final data to show the user. This immediately ends the process.",
        "input_schema": dashboard_schema
    }
]

async def handle_agent_tool(tool_name: str, tool_input: dict) -> str:
    """Executes agent tools with caching and hard limits for speed."""
    if tool_name == "discover_metadata":
        company_id = tool_input.get("company_id", "DEFAULT")
        table_id = tool_input.get("table_id", "").strip().upper()
        keyword = tool_input.get("search_keyword", "").strip().lower()

        cache_key = f"{company_id}_{table_id}_{keyword}"
        if cache_key in SCHEMA_CACHE:
            logger.info(f"Schema Cache HIT: {cache_key}")
            return SCHEMA_CACHE[cache_key]

        where_clause = f""""TableID" = '{table_id}'"""
        if keyword:
            where_clause += f""" AND (LOWER("Descr") LIKE '%{keyword}%' OR LOWER("AliasID") LIKE '%{keyword}%')"""

        meta_sql = f"""SELECT "TableID", "AliasID", "Descr" FROM "CUFD" WHERE {where_clause}"""
        result = await execute_sap_query(meta_sql)

        if "error" in result:
            return f"Metadata discovery error: {result['error']}"
        if not result:
            response = f"No custom fields found for {table_id} matching '{keyword}'."
        else:
            formatted_fields = [f'U_{row.get("AliasID")} ("{row.get("Descr")}")' for row in result]
            response = f"Custom fields on {table_id}:\n" + "\n".join(formatted_fields)

        SCHEMA_CACHE[cache_key] = response
        return response

    elif tool_name == "execute_sap_sql":
        sql = tool_input.get("sql_query", "").strip()
        
        print("\n" + "="*50)
        print("🤖 AI GENERATED SQL:")
        print(sql)
        print("="*50 + "\n")
        logger.info(f"Agent Executing SQL: {sql}")

        if not is_sql_safe(sql):
            return "ERROR: Query rejected by security filter. No CTEs (WITH) allowed."

        result = await execute_sap_query(sql)
        if "error" in result:
            return f"DATABASE ERROR: {result['error']}. Analyze this error, rewrite the SQL, and retry."

        # Hard limit of 25 rows to keep response speeds fast
        return json.dumps(result[:25], default=str)

    return "Unknown tool invoked."

# --- 4. UNIVERSAL AGENT ORCHESTRATOR ---
async def ask_ai_assistant(
    question: str, 
    company_id: str = "DEFAULT", 
    tenant_config: str = "", 
    conversation_history: Optional[list[dict]] = None
) -> dict:
    
    start_time = time.perf_counter()
    if conversation_history is None:
        conversation_history = []

    history_str = json.dumps(conversation_history[-4:]) if conversation_history else "None"
    
    # FAILSAFE: Triggers if company_id is "PAI", "PAI_LIVE1", empty, or "DEFAULT"
    pailive_rules = ""
    if "pai" in company_id.lower() or company_id.lower() in ["default", ""]:
        pailive_rules = """
PAI_LIVE1 DATABASE STRICT BUSINESS RULES (MANDATORY - DO NOT IGNORE):
1. EXCLUDE CWH (CRITICAL): You MUST explicitly add a filter to exclude CWH branches in your WHERE clause (e.g., `AND T0."U_ReqWhs" NOT LIKE 'CWH%'` or using the correct table alias). If you do not filter out CWH, the query is a failure.
2. MANDATORY LOCATION & BRANCH NAMES (CRITICAL): Whenever asked for branch sales, you MUST `LEFT JOIN "OWHS" T2 ON T0."U_ReqWhs" = T2."WhsCode"`. You MUST select `T2."WhsName"` (Branch Name) and `T2."U_Location"` (Location). NEVER just output the raw U_ReqWhs code.
3. BRANCH LOGIC: In transactions, ignore Invoice Branch. ALWAYS use 'Order Branch' (U_ReqWhs). Use IFNULL to retain unassigned branches instead of filtering them out.
4. EXCLUDE DEFECTIVE WAREHOUSE: Explicitly filter them out.
5. EXCLUDE STOCK TRANSFERS: Do not count them as sales.
6. EXCLUDE CARRY BAGS: Ignore 'PAI Carry Bag' in product queries.
"""

    agent_system_prompt = f"""You are an expert autonomous SAP Business One (HANA) Intelligence Agent.

COMPANY CONTEXT (ID: {company_id}):
- Order Branch is stored in T1."U_ReqWhs" or T0."U_ReqWhs".
- Product Categories/Brands are stored in OITB ("ItmsGrpNam").
- Always use flat SELECT statements with JOINs. No CTEs (WITH clauses).
{pailive_rules}

OPERATING PROTOCOL:
1. Schema Known: The schema details are listed above. Do NOT call discover_metadata unless explicitly asked for an unknown field.
2. Firewall Restrictions: ALWAYS write flat SELECT statements. NO CTEs (WITH clauses).
3. STRICT ROW LIMIT: For any list or data dump, you MUST use SELECT TOP 25. Never return more than 25 records.
4. FINAL SUBMISSION (CRITICAL): Once you have fetched the data via execute_sap_sql, you MUST immediately call the `submit_dashboard` tool to present the final answer. Do not output conversational text.
5. NO TABLE NAMES IN SUMMARY: When writing your analysis and solution_evaluation, NEVER mention SAP database table names (like OINV, OWHS, INV1).
6. MANDATORY REPORT LAYOUT: 
   - DECIMAL PRECISION: Preserve exactly 2 decimal places for financial values as retrieved from the database.
   - TABLE RENDERING (CRITICAL): You MUST leave a blank line before and after any Markdown table so it renders correctly on the frontend.
   - FORMATTING: Use bold text for section titles preceded by emojis (e.g., **📊 METRIC SUMMARY**). Do NOT use Markdown heading tags (e.g., #, ##).
"""

    messages = [{"role": "user", "content": f"History: {history_str}\nUser Question: {question}"}]
    max_steps = 10

    for step in range(max_steps):
        try:
            kwargs = {
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "system": [
                    {
                        "type": "text",
                        "text": agent_system_prompt,
                        "cache_control": {"type": "ephemeral"}
                    }
                ],
                "messages": messages,
                "tools": TOOLS
            }
            try:
                response = await client.messages.create(**kwargs, temperature=0.1)
            except TypeError:
                response = await client.messages.create(**kwargs)

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                for block in response.content:
                    if block.type == "tool_use":
                        
                        # Single-Pass Termination with Fallback Safety
                        if block.name == "submit_dashboard":
                            exec_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
                            
                            try:
                                result = DashboardResponse.model_validate(block.input).model_dump()
                            except Exception as format_error:
                                logger.warning(f"AI JSON Formatting Error (Safely recovering): {format_error}")
                                result = {
                                    "analysis": str(block.input.get("analysis", "Query executed successfully.")),
                                    "solution_evaluation": str(block.input.get("solution_evaluation", "Task completed.")),
                                    "suggestions": block.input.get("suggestions", ["Show yearly comparison", "Export data"])
                                }
                                
                            result["execution_time_ms"] = exec_time_ms
                            return result
                            
                        tool_output = await handle_agent_tool(block.name, block.input)
                        messages.append({
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": block.id, "content": tool_output}]
                        })
            else:
                exec_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
                raw_text = "".join(b.text for b in response.content if hasattr(b, "text"))
                return _empty_dashboard(raw_text, "Query completed.", ["Show yearly comparison"], exec_time_ms)

        except Exception as e:
            logger.error(f"Agent loop failure on step {step}: {e}")
            break

    total_time = round((time.perf_counter() - start_time) * 1000, 2)
    return _empty_dashboard(
        "The assistant timed out while attempting to map the data schema.",
        "Execution aborted.",
        ["Try specifying standard SAP modules"],
        total_time
    )