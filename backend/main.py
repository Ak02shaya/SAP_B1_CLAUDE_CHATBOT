import os
from pathlib import Path
from typing import List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Ensure these imports match your actual backend structure
from backend.auth import verify_token, authenticate_user, logout_user, LoginRequest, LoginResponse
from backend.ai_client import ask_ai_assistant

# --- 1. APP INITIALIZATION ---
app = FastAPI(
    title="SAP B1 Autonomous Intelligence Assistant API",
    version="2.0",
    description="Live SAP Business One connector with query execution."
)

# --- 2. CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. PYDANTIC MODELS ---
class ChatRequest(BaseModel):
    # Merged both requirements: validation rules AND conversation history
    question: str = Field(min_length=2, max_length=2000)
    conversation_history: List[Dict[str, Any]] = []

# --- 4. API ROUTES ---
# UPDATED: Added /auth/ to the path to match your script.js
@app.post("/api/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    return authenticate_user(payload)

# UPDATED: Added /auth/ to the path to match your script.js
@app.post("/api/auth/logout")
async def logout(authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        return logout_user(token)
    return {"message": "Already signed out."}

@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, token: str = Depends(verify_token)):
    try:
        # Pass both the question and history to the Claude-powered AI client
        response_data = await ask_ai_assistant(
            question=payload.question,
            conversation_history=payload.conversation_history
        )
        return response_data
    except Exception as e:
        # Optional: Print the actual error to your terminal for developer debugging
        print(f"Chat Endpoint Error: {e}") 
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The assistant could not complete the request. Check the server logs for details."
        )

# --- 5. MOUNT FRONTEND ---
# Using html=True automatically serves index.html at the root URL ("/") without needing a custom @app.get("/") route
frontend_path = Path(__file__).parent.parent / "frontend"

if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    print(f"Warning: 'frontend' directory not found at {frontend_path}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)