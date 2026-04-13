"""
FunPay Worker — запускается в отдельном потоке на каждого юзера.
Слушает события FunPay через Runner.listen(), обрабатывает заказы и сообщения.
"""
import logging
import threading
import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from database import SyncSessionLocal
from models import (
    User, Order, Lot, LotServiceHash, Service, CurrentUserService,
    FunPayOrderStatus, ServiceStatus
)
from services import smm as smm_api
from config import settings

try:
    from FunPayAPI.account import Account
    from FunPayAPI.updater.runner import Runner
    from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
    from FunPayAPI.common.enums import OrderStatuses
except ImportError:
    raise RuntimeError("FunPayAPI не установлен. pip install FunPayAPI")

logger = logging.getLogger("funpay_worker")


# ── Worker Manager ────────────────────────────────────────────────────────────

class WorkerManager:
    """Синглтон. Управляет потоками-воркерами для каждого юзера."""

    def __init__(self):
        self._threads: dict[str, threading.Thread] = {}    # user_id str → Thread
        self._stop_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, user_id: str):
        with self._lock:
            if user_id in self._threads and self._threads[user_id].is_alive():
                logger.info(f"Worker для юзера {user_id} уже запущен")
                return

            stop_event = threading.Event()
            thread = threading.Thread(
                target=_worker_loop,
                args=(user_id, stop_event),
                daemon=True,
                name=f"funpay-worker-{user_id}",
            )
            self._threads[user_id] = thread
            self._stop_events[user_id] = stop_event
            thread.start()
            logger.info(f"Worker для юзера {user_id} запущен")

    def stop(self, user_id: str):
        with self._lock:
            if user_id not in self._stop_events:
                return
            self._stop_events[user_id].set()
            logger.info(f"Worker для юзера {user_id} остановлен")

    def is_running(self, user_id: str) -> bool:
        t = self._threads.get(user_id)
        return t is not None and t.is_alive()

    def start_all_active(self):
        """Вызывается при старте приложения — поднимает воркеры для всех активных юзеров."""
        with SyncSessionLocal() as db:
            users = db.query(User).filter(
                User.golden_key.isnot(None),
                User.smm_key.isnot(None),
            ).all()
            for u in users:
                # Есть ли хоть одна активная автоматизация?
                has_active = db.query(CurrentUserService).filter(
                    CurrentUserService.user_id == u.user_id,
                    CurrentUserService.is_active == True,
                ).first()
                if has_active:
                    self.start(str(u.user_id))


worker_manager = WorkerManager()


# ── Worker Loop ───────────────────────────────────────────────────────────────

def _worker_loop(user_id: str, stop_event: threading.Event):
    logger.info(f"[{user_id}] Worker loop started")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        with SyncSessionLocal() as db:
            user: User = db.query(User).filter(User.user_id == user_id).first()
            if not user or not user.golden_key:
                logger.error(f"[{user_id}] Нет golden_key, останавливаемся")
                return

            try:
                account = Account(user.golden_key).get()
            except Exception as e:
                logger.error(f"[{user_id}] Не удалось инициализировать аккаунт: {e}")
                return

            runner = Runner(account)
            logger.info(f"[{user_id}] Runner инициализирован как {account.username}")

            for event in runner.listen(requests_delay=6.0):
                if stop_event.is_set():
                    break

                try:
                    if isinstance(event, NewOrderEvent):
                        loop.run_until_complete(_handle_new_order(event, user_id, account, db, loop))
                    elif isinstance(event, NewMessageEvent):
                        loop.run_until_complete(_handle_new_message(event, user_id, account, db, loop))
                except Exception as e:
                    logger.error(f"[{user_id}] Ошибка обработки события: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"[{user_id}] Критическая ошибка воркера: {e}", exc_info=True)
    finally:
        loop.close()
        logger.info(f"[{user_id}] Worker loop stopped")


# ── Event Handlers ────────────────────────────────────────────────────────────

async def _handle_new_order(
    event: NewOrderEvent,
    user_id: str,
    account: Account,
    db: Session,
    loop: asyncio.AbstractEventLoop,
):
    order_shortcut = event.order

    logger.debug(f"[{user_id}] NewOrderEvent: id={order_shortcut.id} status={order_shortcut.status} subcategory='{order_shortcut.subcategory_name}' desc='{order_shortcut.description}' amount={order_shortcut.amount} buyer={order_shortcut.buyer_username}")
    # Реагируем ТОЛЬКО на оплаченные заказы
    if order_shortcut.status != OrderStatuses.PAID:
        return

    funpay_order_id = order_shortcut.id

    # Дубль-чек: уже есть в БД?
    existing = db.query(Order).filter(Order.funpay_order_id == funpay_order_id).first()
    if existing:
        return

    # Найти автоматизацию по subcategory_name
    automation = _find_automation(db, user_id, order_shortcut.subcategory_name)
    if not automation:
        logger.info(f"[{user_id}] Нет активной автоматизации для '{order_shortcut.subcategory_name}', игнор")
        return

    # Проверить quantity
    quantity = order_shortcut.amount
    if quantity is None:
        logger.error(f"[{user_id}] order {funpay_order_id}: amount=None — критическая ошибка, пропуск")
        return

    # Создать Order в БД
    order = Order(
        user_id=user_id,
        funpay_order_id=funpay_order_id,
        status=FunPayOrderStatus.PAID,
        subcategory=order_shortcut.subcategory_name,
        short_desc=order_shortcut.description,
        sum_=order_shortcut.price,
        quantity=quantity,
        buyer_id=order_shortcut.buyer_id,
        buyer_username=order_shortcut.buyer_username,
    )
    db.add(order)
    db.flush()

    # Создать Service в статусе pending_input
    service = Service(
        order_id=order.order_id,
        status=ServiceStatus.PENDING_INPUT,
    )
    db.add(service)
    db.commit()

    # Написать покупателю
    _send(account, order_shortcut.buyer_id,
          "Привет! Заказ оплачен ✅\nОтправьте ссылку или ник для выполнения:")
    logger.info(f"[{user_id}] Новый заказ {funpay_order_id} создан, ждём инпут от {order_shortcut.buyer_username}")


async def _handle_new_message(
    event: NewMessageEvent,
    user_id: str,
    account: Account,
    db: Session,
    loop: asyncio.AbstractEventLoop,
):
    msg = event.message

    # Игнорируем свои сообщения
    if msg.by_bot or msg.author_id == account.id:
        return

    text = (msg.text or "").strip()
    buyer_id = msg.author_id
    chat_id = msg.chat_id

    # Найти активный заказ этого покупателя
    order = _get_active_order(db, user_id, buyer_id)
    if not order:
        return

    service: Service = order.service
    if not service:
        return

    # ── /retry ───────────────────────────────────────────────────────────────
    if text == "/retry":
        if service.status in (ServiceStatus.PENDING_INPUT, ServiceStatus.AWAITING_CONFIRM):
            service.status = ServiceStatus.PENDING_INPUT
            order.buyer_input = None
            db.commit()
            _send(account, chat_id, "Хорошо, отправьте новую ссылку/ник:")
        return

    # ── pending_input: ждём ссылку/ник ───────────────────────────────────────
    if service.status == ServiceStatus.PENDING_INPUT:
        if not text:
            return
        order.buyer_input = text
        service.status = ServiceStatus.AWAITING_CONFIRM
        db.commit()
        _send(account, chat_id,
              f"Ваша ссылка/ник:\n{text}\n\n"
              f"Перепроверьте и подтвердите:\n/yes — продолжить\n/no — отмена\n/retry — изменить")
        return

    # ── awaiting_confirm ──────────────────────────────────────────────────────
    if service.status == ServiceStatus.AWAITING_CONFIRM:
        if text == "/yes":
            await _start_smm_order(order, service, db, account, chat_id, user_id, loop)
        elif text == "/no":
            service.status = ServiceStatus.FAILED
            db.commit()
            _send(account, chat_id, "Заказ отменён.")
        return

    # ── processing: /status ───────────────────────────────────────────────────
    if service.status == ServiceStatus.PROCESSING:
        if text == "/status":
            _send(account, chat_id, "⏳ Заказ в работе, ожидайте...")
        return


# ── SMM Order Flow ────────────────────────────────────────────────────────────

async def _start_smm_order(
    order: Order,
    service: Service,
    db: Session,
    account: Account,
    chat_id: int,
    user_id: str,
    loop: asyncio.AbstractEventLoop,
):
    # Найти хэш-запись (lot → smm_service)
    automation = _find_automation(db, user_id, order.subcategory)
    if not automation:
        _send(account, chat_id, "❌ Ошибка конфигурации автоматизации. Свяжитесь с продавцом.")
        service.status = ServiceStatus.FAILED
        db.commit()
        return

    lot_hash: LotServiceHash = db.query(LotServiceHash).filter(
        LotServiceHash.lot_id == automation.lot_id
    ).first()
    if not lot_hash:
        _send(account, chat_id, "❌ Ошибка: SMM сервис не настроен для этого лота.")
        service.status = ServiceStatus.FAILED
        db.commit()
        return

    user: User = db.query(User).filter(User.user_id == user_id).first()
    if not user.smm_key:
        _send(account, chat_id, "❌ Ошибка: SMM ключ не настроен. Свяжитесь с продавцом.")
        service.status = ServiceStatus.FAILED
        db.commit()
        return

    # Обновляем service
    service.smm_service_id = lot_hash.smm_service_id
    service.service_name = lot_hash.service_name
    db.flush()

    # Отправляем заказ в SMM
    try:
        smm_order_id = await smm_api.create_order(
            smm_key=user.smm_key,
            service_id=lot_hash.smm_service_id,
            link=order.buyer_input,
            quantity=order.quantity,
        )
    except smm_api.SMMError as e:
        logger.error(f"SMM create_order failed: {e}")
        _send(account, chat_id, f"❌ Ошибка при создании SMM заказа: {e}\nСвяжитесь с продавцом.")
        service.status = ServiceStatus.FAILED
        db.commit()
        return

    service.smm_order_id = smm_order_id
    service.status = ServiceStatus.PROCESSING
    db.commit()

    _send(account, chat_id, "✅ Заказ принят в работу! Ожидайте.\nМожете проверить статус командой /status")

    # Запускаем поллинг в отдельном потоке
    poll_thread = threading.Thread(
        target=_poll_smm_status,
        args=(str(service.service_id), user_id, chat_id, account.golden_key),
        daemon=True,
    )
    poll_thread.start()


def _poll_smm_status(service_id: str, user_id: str, chat_id: int, golden_key: str):
    """Поллинг статуса SMM заказа. Крутится пока не done/failed."""
    import time

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        account = Account(golden_key).get()

        while True:
            time.sleep(settings.SMM_POLLING_INTERVAL)

            with SyncSessionLocal() as db:
                service: Service = db.query(Service).filter(
                    Service.service_id == service_id
                ).first()

                if not service or service.status in (ServiceStatus.DONE, ServiceStatus.FAILED):
                    break

                user: User = db.query(User).filter(User.user_id == user_id).first()

                try:
                    data = loop.run_until_complete(
                        smm_api.get_status(user.smm_key, service.smm_order_id)
                    )
                except smm_api.SMMError as e:
                    logger.error(f"SMM status poll error: {e}")
                    continue

                smm_status = data.get("status", "")
                logger.info(f"SMM order {service.smm_order_id} status: {smm_status}")

                if smm_status == "Completed":
                    service.status = ServiceStatus.DONE
                    # Обновляем FunPay статус заказа
                    order: Order = service.order
                    order.status = FunPayOrderStatus.CLOSED
                    db.commit()
                    _send(account, chat_id,
                          "✅ Заказ выполнен! Не забудьте подтвердить заказ на FunPay.")
                    break

                elif smm_status in ("Canceled", "Fail", "Partial"):
                    service.status = ServiceStatus.FAILED
                    db.commit()
                    _send(account, chat_id,
                          f"❌ Заказ завершился со статусом: {smm_status}. Свяжитесь с продавцом.")
                    break

                # In progress / Pending — продолжаем поллинг

    except Exception as e:
        logger.error(f"Polling thread crashed: {e}", exc_info=True)
    finally:
        loop.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send(account: Account, chat_id: int, text: str):
    try:
        account.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"send_message failed: {e}")


def _find_automation(db: Session, user_id: str, subcategory_name: str) -> CurrentUserService | None:
    """Ищет активную автоматизацию для данного юзера и подкатегории."""
    return (
        db.query(CurrentUserService)
        .join(Lot, Lot.lot_id == CurrentUserService.lot_id)
        .filter(
            CurrentUserService.user_id == user_id,
            CurrentUserService.is_active == True,
            Lot.subcategory == subcategory_name,
        )
        .first()
    )


def _get_active_order(db: Session, user_id: str, buyer_id: int) -> Order | None:
    """
    Возвращает последний активный (PAID) ордер покупателя у данного продавца,
    у которого сервис ещё не завершён.
    """
    return (
        db.query(Order)
        .join(Service, Service.order_id == Order.order_id)
        .filter(
            Order.user_id == user_id,
            Order.buyer_id == buyer_id,
            Order.status == FunPayOrderStatus.PAID,
            Service.status.in_([
                ServiceStatus.PENDING_INPUT,
                ServiceStatus.AWAITING_CONFIRM,
                ServiceStatus.PROCESSING,
            ]),
        )
        .order_by(Order.created_at.desc())
        .first()
    )
