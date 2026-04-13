"""
FastAPI dependencies.
"""
from fastapi import Header, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User


async def get_current_user(
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Простая аутентификация: фронт передаёт X-User-Id (UUID юзера),
    полученный после /api/auth/login.
    В проде стоит заменить на JWT.
    """
    result = await db.execute(select(User).where(User.user_id == x_user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user
