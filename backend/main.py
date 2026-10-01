import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.auth import verify_token, authenticate_user, logout_user, LoginRequest, LoginResponse
from backend.ai_client import ask_ai_assistant

app = FastAPI(
    title="SAP B1 Autonomous Intelligence Assistant API",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    company_id: Optional[str] = Field(default="PAI_LIVE_1", description="Target SAP tenant ID")
    conversation_history: List[Dict[str, Any]] = []

@app.post("/api/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    return authenticate_user(payload)

@app.post("/api/auth/logout")
async def logout(authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        return logout_user(token)
    return {"message": "Already signed out."}

@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, token_data = Depends(verify_token)):
    try:
        fallback_company_id = getattr(payload, "company_id", "PAI_LIVE_1")
        
        if isinstance(token_data, dict):
            tenant_company_id = token_data.get("company_id", fallback_company_id)
        else:
            tenant_company_id = fallback_company_id
        
        response_data = await ask_ai_assistant(
            question=payload.question,
            company_id=tenant_company_id,
            conversation_history=payload.conversation_history
        )
        return response_data
    except Exception as e:
        print(f"Chat Endpoint Error: {e}") 
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The assistant could not complete the request. Check the server logs for details."
        )

# --- MOUNT FRONTEND STATIC FILES ---
frontend_path = Path(__file__).resolve().parent.parent / "frontend"

if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
else:
    print(f"Warning: 'frontend' directory not found at {frontend_path}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)