"""
Orders router — просмотр заказов и сервисов.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import get_db
from models import User, Order, Service
from deps import get_current_user

router = APIRouter(prefix="/api/orders", tags=["orders"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ServiceOut(BaseModel):
    service_id: str
    smm_service_id: int | None
    smm_order_id: int | None
    status: str
    date: str

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    order_id: str
    funpay_order_id: str
    status: str
    subcategory: str | None
    short_desc: str | None
    sum_: float | None
    quantity: int | None
    buyer_username: str | None
    buyer_input: str | None
    service: ServiceOut | None
    created_at: str

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[OrderOut])
async def get_orders(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order)
        .where(Order.user_id == user.user_id)
        .options(selectinload(Order.service))
        .order_by(Order.created_at.desc())
        .limit(100)
    )
    orders = result.scalars().all()

    return [
        OrderOut(
            order_id=str(o.order_id),
            funpay_order_id=o.funpay_order_id,
            status=o.status.value,
            subcategory=o.subcategory,
            short_desc=o.short_desc,
            sum_=o.sum_,
            quantity=o.quantity,
            buyer_username=o.buyer_username,
            buyer_input=o.buyer_input,
            service=ServiceOut(
                service_id=str(o.service.service_id),
                smm_service_id=o.service.smm_service_id,
                smm_order_id=o.service.smm_order_id,
                status=o.service.status.value,
                date=o.service.date.isoformat(),
            ) if o.service else None,
            created_at=o.created_at.isoformat(),
        )
        for o in orders
    ]
