"""
Automations router — CRUD для current_users_services + lots_to_services_hash_table.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from database import get_db
from models import User, Lot, LotServiceHash, CurrentUserService
from deps import get_current_user

logger = logging.getLogger("automations")
router = APIRouter(prefix="/api/automations", tags=["automations"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class LotData(BaseModel):
    funpay_lot_id: str
    title: str | None = None
    price: float | None = None
    subcategory: str | None = None
    public_link: str | None = None


class AutomationCreate(BaseModel):
    lot: LotData
    smm_service_id: int
    service_name: str | None = None


class AutomationOut(BaseModel):
    id: str
    lot_id: str
    funpay_lot_id: str | None   # добавлено — фронт использует для фильтрации истории
    lot_title: str | None
    lot_subcategory: str | None
    smm_service_id: int
    service_name: str | None
    is_active: bool


class TogglePayload(BaseModel):
    is_active: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[AutomationOut])
async def list_automations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CurrentUserService)
        .where(CurrentUserService.user_id == user.user_id)
        .options(selectinload(CurrentUserService.lot))
    )
    automations = result.scalars().all()
    return [_to_out(a) for a in automations]


@router.post("/", response_model=AutomationOut)
async def create_automation(
    payload: AutomationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Проверяем: есть ли уже лот с таким funpay_lot_id у этого юзера
    existing_lot_q = await db.execute(
        select(Lot).where(
            Lot.user_id == user.user_id,
            Lot.funpay_lot_id == payload.lot.funpay_lot_id,
        )
    )
    lot = existing_lot_q.scalar_one_or_none()

    if lot:
        # Проверяем нет ли уже автоматизации для этого лота
        existing_auto = await db.execute(
            select(CurrentUserService).where(
                CurrentUserService.user_id == user.user_id,
                CurrentUserService.lot_id == lot.lot_id,
            )
        )
        if existing_auto.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Автоматизация для этого лота уже существует")
    else:
        lot = Lot(
            user_id=user.user_id,
            funpay_lot_id=payload.lot.funpay_lot_id,
            title=payload.lot.title,
            price=payload.lot.price,
            subcategory=payload.lot.subcategory,
            public_link=payload.lot.public_link,
        )
        db.add(lot)
        await db.flush()

    # Сохраняем хэш lot → smm_service (перезаписываем если был)
    await db.execute(delete(LotServiceHash).where(LotServiceHash.lot_id == lot.lot_id))
    lot_hash = LotServiceHash(
        lot_id=lot.lot_id,
        smm_service_id=payload.smm_service_id,
        service_name=payload.service_name,
    )
    db.add(lot_hash)

    # Создаём автоматизацию
    automation = CurrentUserService(
        user_id=user.user_id,
        lot_id=lot.lot_id,
        smm_service_id=payload.smm_service_id,
        is_active=True,
    )
    db.add(automation)
    await db.commit()
    await db.refresh(automation)
    await db.refresh(automation, ["lot"])

    from services.funpay_worker import worker_manager
    worker_manager.start(str(user.user_id))

    return _to_out(automation)


@router.patch("/{automation_id}/toggle", response_model=AutomationOut)
async def toggle_automation(
    automation_id: str,
    payload: TogglePayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CurrentUserService)
        .where(
            CurrentUserService.id == automation_id,
            CurrentUserService.user_id == user.user_id,
        )
        .options(selectinload(CurrentUserService.lot))
    )
    automation = result.scalar_one_or_none()
    if not automation:
        raise HTTPException(status_code=404, detail="Автоматизация не найдена")

    automation.is_active = payload.is_active
    await db.commit()

    return _to_out(automation)


@router.delete("/{automation_id}")
async def delete_automation(
    automation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CurrentUserService).where(
            CurrentUserService.id == automation_id,
            CurrentUserService.user_id == user.user_id,
        )
    )
    automation = result.scalar_one_or_none()
    if not automation:
        raise HTTPException(status_code=404, detail="Автоматизация не найдена")

    lot_id = automation.lot_id
    await db.execute(delete(LotServiceHash).where(LotServiceHash.lot_id == lot_id))
    await db.delete(automation)
    await db.flush()
    # Лот не удаляем — к нему привязана история заказов
    await db.commit()
    return {"ok": True}


# ── Helper ────────────────────────────────────────────────────────────────────

def _to_out(a: CurrentUserService) -> AutomationOut:
    return AutomationOut(
        id=str(a.id),
        lot_id=str(a.lot_id),
        funpay_lot_id=a.lot.funpay_lot_id if a.lot else None,
        lot_title=a.lot.title if a.lot else None,
        lot_subcategory=a.lot.subcategory if a.lot else None,
        smm_service_id=a.smm_service_id,
        service_name=None,
        is_active=a.is_active,
    )
