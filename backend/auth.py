from datetime import datetime, timedelta, timezone
import secrets

from fastapi import Security, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()

security = HTTPBearer(auto_error=False)

ADMIN_USERNAME = os.getenv("SAP_ADMIN_USER")
ADMIN_PASSWORD = os.getenv("SAP_ADMIN_PASSWORD")
SESSION_TTL_MINUTES = int(os.getenv("SAP_SESSION_TTL_MINUTES", "60"))

ACTIVE_SESSIONS: dict[str, datetime] = {}

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    message: str

def authenticate_user(credentials: LoginRequest) -> LoginResponse:
    if ADMIN_USERNAME and ADMIN_PASSWORD and credentials.username == ADMIN_USERNAME and credentials.password == ADMIN_PASSWORD:
        token = secrets.token_urlsafe(32)
        ACTIVE_SESSIONS[token] = datetime.now(timezone.utc) + timedelta(minutes=SESSION_TTL_MINUTES)
        return LoginResponse(
            access_token=token,
            token_type="bearer",
            message="Sign in successful."
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password."
    )

def logout_user(token: str) -> dict:
    ACTIVE_SESSIONS.pop(token, None)
    return {"message": "Successfully signed out."}

async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    token = credentials.credentials
    expires_at = ACTIVE_SESSIONS.get(token)
    if not expires_at or expires_at <= datetime.now(timezone.utc):
        ACTIVE_SESSIONS.pop(token, None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid token. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token