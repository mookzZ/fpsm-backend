import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, ForeignKey, Text, Enum as SAEnum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from database import Base


# ── Enums ────────────────────────────────────────────────────────────────────

class FunPayOrderStatus(str, enum.Enum):
    PAID = "PAID"
    CLOSED = "CLOSED"
    REFUNDED = "REFUNDED"


class ServiceStatus(str, enum.Enum):
    PENDING_INPUT = "pending_input"
    AWAITING_CONFIRM = "awaiting_confirm"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


# ── Tables ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    user_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(128), nullable=True)
    golden_key = Column(String(512), nullable=True)
    smm_key = Column(String(512), nullable=True)
    permission = Column(String(64), default="free")
    expires_in = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    orders = relationship("Order", back_populates="user")
    lots = relationship("Lot", back_populates="user")
    automations = relationship("CurrentUserService", back_populates="user")


class Order(Base):
    __tablename__ = "orders"

    order_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    funpay_order_id = Column(String(64), unique=True, nullable=False, index=True)
    buyer_input = Column(Text, nullable=True)  # ник/ссылка от покупателя
    full_desc = Column(Text, nullable=True)  # подробное описание с FunPay (содержит id: XXXXX)
    status = Column(SAEnum(FunPayOrderStatus), nullable=False, default=FunPayOrderStatus.PAID)
    subcategory = Column(String(256), nullable=True)
    short_desc = Column(Text, nullable=True)
    sum_ = Column(Float, nullable=True)
    quantity = Column(Integer, nullable=True)
    buyer_id = Column(Integer, nullable=True)
    buyer_username = Column(String(128), nullable=True)
    seller_id = Column(Integer, nullable=True)
    seller_username = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="orders")
    service = relationship("Service", back_populates="order", uselist=False)


class Lot(Base):
    __tablename__ = "lots"

    lot_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    funpay_lot_id = Column(String(64), nullable=False)
    title = Column(String(512), nullable=True)
    price = Column(Float, nullable=True)
    subcategory = Column(String(256), nullable=True)
    public_link = Column(String(512), nullable=True)

    user = relationship("User", back_populates="lots")
    hash_entries = relationship("LotServiceHash", back_populates="lot")
    automations = relationship("CurrentUserService", back_populates="lot")


class LotServiceHash(Base):
    """Связывает lot_id с smm_service_id."""
    __tablename__ = "lots_to_services_hash_table"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.lot_id"), nullable=False)
    smm_service_id = Column(Integer, nullable=False)
    service_name = Column(String(256), nullable=True)

    lot = relationship("Lot", back_populates="hash_entries")


class Service(Base):
    """Запись о выполнении SMM-заказа по конкретному ордеру."""
    __tablename__ = "services"

    service_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.order_id"), unique=True, nullable=False)
    date = Column(DateTime, default=datetime.utcnow)
    service_name = Column(String(256), nullable=True)
    smm_service_id = Column(Integer, nullable=True)
    smm_order_id = Column(Integer, nullable=True)  # id от smmway после action=add
    status = Column(SAEnum(ServiceStatus), nullable=False, default=ServiceStatus.PENDING_INPUT)

    order = relationship("Order", back_populates="service")


class CurrentUserService(Base):
    """Активные автоматизации юзера: лот → SMM-сервис."""
    __tablename__ = "current_users_services"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.lot_id"), nullable=False)
    smm_service_id = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="automations")
    lot = relationship("Lot", back_populates="automations")
