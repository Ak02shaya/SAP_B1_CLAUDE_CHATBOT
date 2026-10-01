import os
import json
import re
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from backend.sap_client import execute_sap_query
from backend.rule_enforcer import check_business_rules, is_sql_safe, slice_rows

load_dotenv()
logger = logging.getLogger("sap_ai_assistant")
logging.basicConfig(level=logging.WARNING)

client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")

# Load Tenant Config
CONFIG_PATH = Path(__file__).parent / "tenant_config.json"
with open(CONFIG_PATH, "r") as f:
    TENANT_CONFIG = json.load(f)

class DashboardResponse(BaseModel):
    analysis: str = Field(description="Structured dashboard report formatted strictly with Markdown tables and visual sections.")
    solution_evaluation: str = Field(description="Key observations and analytical takeaway without table names.")
    suggestions: list[str] = Field(description="3 context-aware follow-up business queries.")

dashboard_schema = DashboardResponse.model_json_schema()
dashboard_schema.pop("title", None)

def _scrub_markdown_formatting(text: str) -> str:
    if not text: return text
    text = re.sub(r'^(#{1,6})\s+(.+)$', r'**\2**', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    return text

def _empty_dashboard(analysis: str, evaluation: str, suggestions: list[str], exec_ms: float = 0.0) -> dict:
    return {
        "analysis": _scrub_markdown_formatting(analysis),
        "solution_evaluation": _scrub_markdown_formatting(evaluation),
        "suggestions": suggestions,
        "execution_time_ms": exec_ms,
    }

TOOLS = [
    {
        "name": "execute_sap_sql",
        "description": "Executes a single read-only SELECT statement on SAP HANA.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql_query": {"type": "string", "description": "Flat HANA SELECT query. NO WITH clauses."}
            },
            "required": ["sql_query"]
        }
    },
    {
        "name": "submit_dashboard",
        "description": "Call this tool ONLY after a successful execute_sap_sql call.",
        "input_schema": dashboard_schema
    }
]

async def handle_agent_tool(tool_name: str, tool_input: dict, company_id: str, session_state: dict) -> str:
    if tool_name == "execute_sap_sql":
        sql = tool_input.get("sql_query", "").strip()
        
        if not is_sql_safe(sql):
            return "ERROR: Query rejected by security filter. No CTEs (WITH) allowed."
            
        # Pass company_id to the rule enforcer so it knows which JSON rules to apply
        rule_violations = check_business_rules(sql, company_id)
        if rule_violations:
            return "RULES VIOLATED. Please fix the SQL and retry:\n- " + "\n- ".join(rule_violations)

        result = await execute_sap_query(sql, company_id=company_id)
        if isinstance(result, dict) and "error" in result:
            return f"DATABASE ERROR: {result['error']}. Fix the query and retry."

        session_state["final_executed_sql"] = sql
        sliced_result = slice_rows(result, 25)
        return json.dumps(sliced_result, default=str)

    return "Unknown tool invoked."

async def ask_ai_assistant(
    question: str, 
    company_id: str = "DEFAULT", 
    conversation_history: Optional[list[dict]] = None
) -> dict:
    
    start_time = time.perf_counter()
    session_state = {"final_executed_sql": ""}
    
    session_input_tokens = 0
    session_output_tokens = 0

    current_date = datetime.utcnow().strftime("%Y-%m-%d")
    current_year = datetime.utcnow().year
    
    # Extract Specific Rules for this Company
    tenant_rules = TENANT_CONFIG.get(company_id, TENANT_CONFIG["DEFAULT"])
    brand_logic = tenant_rules.get("brand_logic", "")
    location_logic = tenant_rules.get("location_logic", "")
    exclusions_list = tenant_rules.get("ai_prompt_exclusions", [])
    exclusions_text = "\n    - ".join(exclusions_list) if exclusions_list else "None specific."

    agent_system_prompt = f"""You are an expert SAP Business One HANA SQL Intelligence Agent operating on the {company_id} database.
Current Database Context: Today is {current_date} (Year: {current_year}). Use this to resolve relative dates.

1. Core Architecture
- Stateless Operation: You are purely stateless. Do not store or rely on historical queries.
- Error Resilience: If a tool returns a RULE VIOLATION, silently fix the SQL and retry.

2. Specific SAP B1 HANA SQL Rules
- Top 25 Limit: Every query MUST start with SELECT TOP 25.
- Raw Numerical Data: FORBIDDEN to use ROUND() or CAST() on revenue, totals, or quantities. Pull raw decimals.
- Case-Insensitive Searches: Wrap both sides of text filter conditions in LOWER().
- Standard Table Aliases: T0 (OINV), T1 (INV1), T2 (OITM), OWHS (Warehouses), OCRD (Customers).

3. DIMENSIONAL MAPPING & COMPANY RULES
- BRANDS & CATEGORIES:
  * Categories/Groups: Join T2 to OITB on T2."ItmsGrpCod" = OITB."ItmsGrpCod".
  * Brand/Manufacturer: {brand_logic}
- GEOGRAPHY:
  * Branch/Store: {location_logic}
  * Customer: Extract from OCRD (e.g., OCRD."State2", OCRD."City").
- TEMPORAL: Explicitly permitted to use YEAR(T0."DocDate") and MONTH(T0."DocDate").

4. MANDATORY REVENUE INTEGRITY RULES
- Line-Level Warehouse: ALWAYS join `INNER JOIN OWHS ON T1."WhsCode" = OWHS."WhsCode"`.
- FORBIDDEN: Header-level fields like OINV."U_ReqWhs".
- Accurate Totals: Always use `SUM(T1."LineTotal")`, NEVER `SUM(T0."DocTotal")`.
- Active Invoices Only: T0."CANCELED" = 'N' AND T0."DocType" = 'I'.
- Sales BOM Bundles: T1."TreeType" <> 'S'
- Company Specific Exclusions: 
    - {exclusions_text}

5. Output Formatting
- Clean UI Response: Do not use raw markdown heading tags (#) or blockquotes (>). Use bold text.
- Simple Language: Zero database table names or SQL syntax in the final UI.
"""

    messages = [{"role": "user", "content": question}]
    max_steps = 4 

    for step in range(max_steps):
        try:
            kwargs = {
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "system": [{"type": "text", "text": agent_system_prompt, "cache_control": {"type": "ephemeral"}}],
                "messages": messages,
                "tools": TOOLS
            }
            
            response = await client.messages.create(**kwargs)

            if hasattr(response, "usage") and response.usage:
                session_input_tokens += getattr(response.usage, "input_tokens", 0)
                session_output_tokens += getattr(response.usage, "output_tokens", 0)

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                for block in response.content:
                    if block.type == "tool_use":
                                
                        if block.name == "submit_dashboard":
                            exec_time_sec = round(time.perf_counter() - start_time, 2)
                            total_tokens = session_input_tokens + session_output_tokens
                            
                            print("\n" + "="*50)
                            print("🏆 FINAL EXECUTED SQL QUERY:")
                            print(session_state["final_executed_sql"] if session_state["final_executed_sql"] else "No SQL generated.")
                            print("\n📊 TOKEN USAGE:")
                            print(f"   - Input Tokens  : {session_input_tokens}")
                            print(f"   - Output Tokens : {session_output_tokens}")
                            print(f"   - Total Tokens  : {total_tokens}")
                            print(f"⏱️ Execution Time : {exec_time_sec} seconds")
                            print("="*50 + "\n")

                            try:
                                result = DashboardResponse.model_validate(block.input).model_dump()
                            except Exception as format_error:
                                logger.warning(f"Formatting recovery: {format_error}")
                                result = {
                                    "analysis": str(block.input.get("analysis", "Query executed successfully.")),
                                    "solution_evaluation": str(block.input.get("solution_evaluation", "Task completed.")),
                                    "suggestions": block.input.get("suggestions", ["Show sales summary"])
                                }
                                
                            result["analysis"] = _scrub_markdown_formatting(result["analysis"])
                            result["solution_evaluation"] = _scrub_markdown_formatting(result["solution_evaluation"])
                            result["execution_time_ms"] = exec_time_sec
                            return result
                            
                        tool_output = await handle_agent_tool(block.name, block.input, company_id, session_state)
                        messages.append({
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": block.id, "content": tool_output}]
                        })
            else:
                exec_time_sec = round(time.perf_counter() - start_time, 2)
                raw_text = "".join(b.text for b in response.content if hasattr(b, "text"))
                return _empty_dashboard(raw_text, "Query completed.", ["Show sales summary"], exec_time_sec)

        except Exception as e:
            logger.error(f"Agent loop failure on step {step}: {e}")
            break

    total_time = round(time.perf_counter() - start_time, 2)
    return _empty_dashboard(
        "The assistant reached its execution step limit while processing the request.",
        "Execution aborted.",
        ["Try rephrasing your question"],
        total_time
    )