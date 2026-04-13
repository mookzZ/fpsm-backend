"""
Auth router.
Telegram Mini App отправляет initData, мы верифицируем и возвращаем юзера.
"""
import hashlib
import hmac
import json
import logging
from urllib.parse import unquote, parse_qsl

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from database import get_db
from models import User

logger = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

BOT_TOKEN = settings.SECRET_KEY


# ── Schemas ───────────────────────────────────────────────────────────────────

class InitDataPayload(BaseModel):
    init_data: str


class UserOut(BaseModel):
    user_id: str
    telegram_id: int
    username: str | None
    permission: str
    expires_in: str | None
    has_golden_key: bool
    has_smm_key: bool

    class Config:
        from_attributes = True


# ── Utils ─────────────────────────────────────────────────────────────────────

def verify_init_data(init_data: str, bot_token: str) -> dict:
    """Верифицирует Telegram WebApp initData. Raises ValueError если невалидно."""
    parsed = dict(parse_qsl(unquote(init_data), keep_blank_values=True))
    hash_received = parsed.pop("hash", None)
    if not hash_received:
        raise ValueError("hash missing")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, hash_received):
        raise ValueError("invalid hash")

    return json.loads(parsed.get("user", "{}"))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login", response_model=UserOut)
async def login(payload: InitDataPayload, db: AsyncSession = Depends(get_db)):
    # Dev режим: если init_data == "dev" — пропускаем верификацию
    if payload.init_data == "dev":
        tg_user = {"id": 0, "username": "dev"}
    else:
        try:
            tg_user = verify_init_data(payload.init_data, BOT_TOKEN)
        except ValueError as e:
            raise HTTPException(status_code=401, detail=f"Invalid initData: {e}")

    telegram_id = tg_user.get("id")
    username = tg_user.get("username")

    if not telegram_id and telegram_id != 0:
        raise HTTPException(status_code=400, detail="No user id in initData")

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if not user:
        user = User(telegram_id=telegram_id, username=username)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        if username and user.username != username:
            user.username = username
            await db.commit()

    return UserOut(
        user_id=str(user.user_id),
        telegram_id=user.telegram_id,
        username=user.username,
        permission=user.permission,
        expires_in=user.expires_in.isoformat() if user.expires_in else None,
        has_golden_key=bool(user.golden_key),
        has_smm_key=bool(user.smm_key),
    )
