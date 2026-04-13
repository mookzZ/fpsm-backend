"""
Lots router.
Сохранение golden_key/smm_key, получение лотов с FunPay без сохранения в БД.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User
from deps import get_current_user

try:
    from FunPayAPI.account import Account
except ImportError:
    raise RuntimeError("FunPayAPI not installed")

logger = logging.getLogger("lots")
router = APIRouter(prefix="/api/lots", tags=["lots"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class KeysPayload(BaseModel):
    golden_key: str
    smm_key: str


class LotOut(BaseModel):
    funpay_lot_id: str
    title: str | None
    price: float | None
    subcategory: str | None
    public_link: str | None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/keys")
async def save_keys(
    payload: KeysPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сохраняет golden_key и smm_key, валидирует golden_key через FunPay."""
    try:
        account = Account(payload.golden_key).get()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Невалидный golden_key: {e}")

    user.golden_key = payload.golden_key
    user.smm_key = payload.smm_key
    await db.commit()

    # Запускаем воркер
    from services.funpay_worker import worker_manager
    worker_manager.start(str(user.user_id))

    return {"ok": True, "username": account.username}


@router.get("/from-funpay", response_model=list[LotOut])
async def get_lots_from_funpay(
    user: User = Depends(get_current_user),
):
    """
    Фетчит лоты напрямую с FunPay без сохранения в БД.
    Фронт показывает их юзеру для выбора.
    """
    if not user.golden_key:
        raise HTTPException(status_code=400, detail="golden_key не настроен")

    try:
        account = Account(user.golden_key).get()
        profile = account.get_user(account.id)
        fp_lots = profile.get_lots()
    except Exception as e:
        logger.error(f"FunPay get_lots failed: {e}")
        raise HTTPException(status_code=502, detail=f"Ошибка получения лотов с FunPay: {e}")

    return [
        LotOut(
            funpay_lot_id=str(fl.id),
            title=fl.title or fl.description,
            price=fl.price,
            subcategory=fl.subcategory.name if fl.subcategory else None,
            public_link=fl.public_link,
        )
        for fl in fp_lots
    ]
