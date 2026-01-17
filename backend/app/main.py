from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any, Dict, cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api import (
  calendar,
  deliveries,
  finance,
  gasoline,
  import_excel,
  predictions,
  restock_orders,
  sales,
  inventory,
  notifications,
  suppliers,
  settings as settings_api,
  shop_inventory,
  staffing,
)


class AuthSettings:
    def __init__(self) -> None:
        secret_key = os.getenv("SECRET_KEY")
        if not secret_key:
            raise RuntimeError("SECRET_KEY is required.")
        self.secret_key = secret_key
        self.access_token_exp_minutes: int = int(os.getenv("ACCESS_TOKEN_EXP_MIN", "15"))
        self.refresh_token_exp_minutes: int = int(os.getenv("REFRESH_TOKEN_EXP_MIN", str(60 * 24 * 7)))
        roles = os.getenv("TEST_ROLES", "admin")
        username = os.getenv("TEST_USERNAME")
        password = os.getenv("TEST_PASSWORD")
        if not username or not password:
            raise RuntimeError("TEST_USERNAME and TEST_PASSWORD must be set.")
        self.primary_user: Dict[str, Any] = {
            "username": username,
            "password": password,
            "roles": [role.strip() for role in roles.split(",") if role.strip()] or ["admin"],
        }


auth_settings = AuthSettings()

app = FastAPI(title="Gasolinera API")
app.include_router(gasoline.router)
app.include_router(calendar.router)
app.include_router(finance.router)
app.include_router(settings_api.router)
app.include_router(shop_inventory.router)
app.include_router(inventory.router)
app.include_router(staffing.router)
app.include_router(notifications.router)
app.include_router(deliveries.router)
app.include_router(restock_orders.router)
app.include_router(suppliers.router)
app.include_router(import_excel.router)
app.include_router(sales.router)
app.include_router(predictions.router)
reminder_task = None

# Start periodic Excel refresh (every 5 minutes)
import_excel.start_excel_refresh(interval_seconds=300)


@app.middleware("http")
async def enforce_auth(request: Request, call_next):  # type: ignore[override]
  path = request.url.path
  if not path.startswith("/api"):
    return await call_next(request)
  if path.startswith("/api/health") or path.startswith("/api/auth/"):
    return await call_next(request)

  auth_header = request.headers.get("authorization", "")
  if not auth_header.lower().startswith("bearer "):
    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Missing or invalid token."})

  token = auth_header.split(" ", 1)[1].strip()
  if not token:
    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Missing or invalid token."})

  try:
    payload = _decode_token(token)
  except HTTPException as exc:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

  request.state.user = payload
  return await call_next(request)


class LoginRequest(BaseModel):
  username: str
  password: str


class TokenUser(BaseModel):
  id: str
  username: str
  roles: list[str]


class TokenResponse(BaseModel):
  accessToken: str
  refreshToken: str
  user: TokenUser


class RefreshRequest(BaseModel):
  refreshToken: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _encode_token(payload: Dict[str, Any]) -> str:
  header = {"alg": "HS256", "typ": "JWT"}
  header_segment = _b64url(json.dumps(header, separators=(",", ":")).encode())
  payload_segment = _b64url(json.dumps(payload, separators=(",", ":")).encode())
  signing_input = f"{header_segment}.{payload_segment}".encode()
  signature = hmac.new(auth_settings.secret_key.encode(), signing_input, hashlib.sha256).digest()
  return f"{header_segment}.{payload_segment}.{_b64url(signature)}"


def _decode_token(token: str) -> Dict[str, Any]:
  try:
    header_segment, payload_segment, signature_segment = token.split(".")
  except ValueError as exc:  # pragma: no cover - defensive guard
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token") from exc

  signing_input = f"{header_segment}.{payload_segment}".encode()

  def _b64decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)

  signature = _b64decode(signature_segment)
  expected_signature = hmac.new(auth_settings.secret_key.encode(), signing_input, hashlib.sha256).digest()
  if not hmac.compare_digest(signature, expected_signature):
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")

  payload_bytes = _b64decode(payload_segment)
  payload = json.loads(payload_bytes)

  exp = payload.get("exp")
  if exp is not None and int(exp) < int(time.time()):
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")

  return cast(Dict[str, Any], payload)


def _verify_credentials(request: LoginRequest) -> TokenUser:
  username = request.username.strip()
  password = request.password

  candidate = auth_settings.primary_user

  if not (username.lower() == candidate["username"].lower() and password == candidate["password"]):
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

  return TokenUser(id=str(uuid.uuid4()), username=candidate["username"], roles=candidate["roles"])


@app.post("/api/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
  user = _verify_credentials(payload)
  now = int(time.time())
  access_payload = {
    "sub": user.id,
    "username": user.username,
    "roles": user.roles,
    "iat": now,
    "exp": now + auth_settings.access_token_exp_minutes * 60,
  }
  refresh_payload = {
    "sub": user.id,
    "username": user.username,
    "roles": user.roles,
    "iat": now,
    "exp": now + auth_settings.refresh_token_exp_minutes * 60,
    "type": "refresh",
  }
  return TokenResponse(
    accessToken=_encode_token(access_payload),
    refreshToken=_encode_token(refresh_payload),
    user=user,
  )


@app.get("/api/auth/login")
def login_info() -> dict[str, str]:
  return {"detail": "POST credentials to this endpoint to obtain tokens."}


@app.post("/api/auth/refresh", response_model=TokenResponse)
def refresh_tokens(payload: RefreshRequest) -> TokenResponse:
  token_payload = _decode_token(payload.refreshToken)
  if token_payload.get("type") != "refresh":
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

  user = TokenUser(
    id=token_payload.get("sub", str(uuid.uuid4())),
    username=token_payload.get("username", auth_settings.primary_user["username"]),
    roles=token_payload.get("roles", auth_settings.primary_user["roles"]),
  )

  now = int(time.time())
  access_payload = {
    "sub": user.id,
    "username": user.username,
    "roles": user.roles,
    "iat": now,
    "exp": now + auth_settings.access_token_exp_minutes * 60,
  }
  refresh_payload = {
    "sub": user.id,
    "username": user.username,
    "roles": user.roles,
    "iat": now,
    "exp": now + auth_settings.refresh_token_exp_minutes * 60,
    "type": "refresh",
  }
  return TokenResponse(
    accessToken=_encode_token(access_payload),
    refreshToken=_encode_token(refresh_payload),
    user=user,
  )


@app.get("/api/health")
def health() -> dict[str, str]:
  return {"status": "ok"}


@app.on_event("startup")
async def start_calendar_reminders() -> None:
  import asyncio
  global reminder_task
  # Run one immediate pass
  try:
    calendar._dispatch_calendar_email_reminders()
  except Exception:
    pass
  reminder_task = asyncio.create_task(calendar.reminder_loop())


@app.on_event("shutdown")
async def stop_calendar_reminders() -> None:
  global reminder_task
  if reminder_task:
    reminder_task.cancel()
    try:
      await reminder_task
    except Exception:
      pass
