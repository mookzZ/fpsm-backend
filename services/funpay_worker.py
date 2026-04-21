"""
FunPay Worker — запускается в отдельном потоке на каждого юзера.
Слушает события FunPay через Runner.listen(), обрабатывает заказы и сообщения.
Каждый handler диспатчится в ThreadPoolExecutor — независимая параллельная обработка заказов.
"""
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session, joinedload

from database import SyncSessionLocal
from models import (
    User, Order, Lot, LotServiceHash, Service, CurrentUserService,
    FunPayOrderStatus, ServiceStatus
)
from services import smm as smm_api
from config import settings

try:
    import requests as _requests
    import FunPayAPI.account as _fp_account

    # Патч: FunPayAPI шлёт запросы без locale — сайт отвечает /en/ и парсер не находит элементы.
    # Принудительно выставляем locale=ru и нормальный User-Agent на каждую сессию.
    _OrigSession = _requests.Session

    class _PatchedSession(_OrigSession):
        _UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cookies.set("locale", "ru", domain="funpay.com")
            self.headers["User-Agent"] = self._UA

    _requests.Session = _PatchedSession
    _fp_account.requests.Session = _PatchedSession

    from FunPayAPI.account import Account
    from FunPayAPI.updater.runner import Runner
    from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
    from FunPayAPI.common.enums import OrderStatuses

    from bs4 import BeautifulSoup as _BS
    from FunPayAPI import types as _fp_types

    _orig_parse_messages = _fp_account.Account._Account__parse_messages

    def _fixed_parse_messages(self, json_messages, chat_id, interlocutor_id=None,
                               interlocutor_username=None, from_id=0):
        messages = []
        ids = {self.id: self.username, 0: "FunPay"}
        badges = {}
        if interlocutor_id is not None:
            ids[interlocutor_id] = interlocutor_username

        bot_char = self._Account__bot_character

        for i in json_messages:
            if i["id"] < from_id:
                continue
            author_id = i["author"]
            parser = _BS(i["html"], "html.parser")

            if None in [ids.get(author_id), badges.get(author_id)] and (
                    author_div := parser.find("div", {"class": "media-user-name"})):
                if badges.get(author_id) is None:
                    badge = author_div.find("span")
                    badges[author_id] = badge.text if badge else 0
                if ids.get(author_id) is None:
                    a_tag = author_div.find("a")
                    if a_tag:
                        author = a_tag.text.strip()
                        ids[author_id] = author
                        if self.chat_id_private and author_id == interlocutor_id and not interlocutor_username:
                            interlocutor_username = author
                            ids[interlocutor_id] = interlocutor_username

            if self.chat_id_private and (image_link := parser.find("a", {"class": "chat-img-link"})):
                image_link = image_link.get("href")
                message_text = None
            else:
                image_link = None
                if author_id == 0:
                    el = parser.find("div", {"class": "alert alert-with-icon alert-info"})
                    message_text = el.text.strip() if el else ""
                else:
                    el = (parser.find("div", {"class": "message-text"}) or
                          parser.find("div", {"class": "chat-msg-text"}))
                    message_text = el.text if el else ""

            by_bot = False
            if not image_link and message_text and message_text.startswith(bot_char):
                message_text = message_text.replace(bot_char, "", 1)
                by_bot = True

            message_obj = _fp_types.Message(
                i["id"], message_text, chat_id, interlocutor_username,
                None, author_id, i["html"], image_link, determine_msg_type=False
            )
            message_obj.by_bot = by_bot
            message_obj.type = (_fp_types.MessageTypes.NON_SYSTEM if author_id != 0
                                 else message_obj.get_message_type())
            messages.append(message_obj)

        return messages

    _fp_account.Account._Account__parse_messages = _fixed_parse_messages

    # Патч: FunPayAPI Runner крашится на messages[-1] когда список пустой.
    # Это происходит когда все сообщения в ответе уже старше from_id (например после отправки боtом).
    from FunPayAPI.updater import runner as _fp_runner
    _orig_generate_new_message_events = _fp_runner.Runner.generate_new_message_events

    def _safe_generate_new_message_events(self, obj):
        try:
            return _orig_generate_new_message_events(self, obj)
        except (IndexError, KeyError, TypeError):
            return []

    _fp_runner.Runner.generate_new_message_events = _safe_generate_new_message_events

except ImportError:
    raise RuntimeError("FunPayAPI не установлен. pip install FunPayAPI")

logger = logging.getLogger("funpay_worker")


# ── Worker Manager ────────────────────────────────────────────────────────────

class WorkerManager:
    """Синглтон. Управляет потоками-воркерами для каждого юзера."""

    def __init__(self):
        self._threads: dict[str, threading.Thread] = {}
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

    with SyncSessionLocal() as db:
        user: User = db.query(User).filter(User.user_id == user_id).first()
        if not user or not user.golden_key:
            logger.error(f"[{user_id}] Нет golden_key, останавливаемся")
            return
        golden_key = user.golden_key

    try:
        account = Account(golden_key).get()
    except Exception as e:
        logger.error(f"[{user_id}] Не удалось инициализировать аккаунт: {e}")
        return

    account_lock = threading.Lock()
    runner = Runner(account)
    logger.info(f"[{user_id}] Runner инициализирован как {account.username}")

    try:
        with ThreadPoolExecutor(max_workers=20, thread_name_prefix=f"fp-{user_id}") as executor:
            for event in runner.listen(requests_delay=1.5):
                if stop_event.is_set():
                    break
                try:
                    if isinstance(event, NewOrderEvent):
                        executor.submit(_handle_new_order, event, user_id, account, account_lock)
                    elif isinstance(event, NewMessageEvent):
                        executor.submit(_handle_new_message, event, user_id, account, account_lock)
                except Exception as e:
                    logger.error(f"[{user_id}] Ошибка диспатча события: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"[{user_id}] Критическая ошибка воркера: {e}", exc_info=True)
    finally:
        logger.info(f"[{user_id}] Worker loop stopped")


# ── Event Handlers ────────────────────────────────────────────────────────────

def _handle_new_order(
    event: NewOrderEvent,
    user_id: str,
    account: Account,
    account_lock: threading.Lock,
):
    order_shortcut = event.order

    logger.debug(f"[{user_id}] NewOrderEvent: id={order_shortcut.id} status={order_shortcut.status} subcategory='{order_shortcut.subcategory_name}' desc='{order_shortcut.description}' amount={order_shortcut.amount} buyer={order_shortcut.buyer_username}")

    if order_shortcut.status != OrderStatuses.PAID:
        return

    funpay_order_id = order_shortcut.id

    with account_lock:
        try:
            full_order = account.get_order(funpay_order_id)
        except Exception as e:
            logger.error(f"[{user_id}] Не удалось получить полный ордер {funpay_order_id}: {e}")
            return
    full_desc = full_order.full_description or ""
    logger.debug(f"[{user_id}] full_description: {repr(full_desc)}")

    with SyncSessionLocal() as db:
        if db.query(Order).filter(Order.funpay_order_id == funpay_order_id).first():
            return

        automation = _find_automation(db, user_id, full_desc)
        if not automation:
            logger.info(f"[{user_id}] Нет активной автоматизации для ордера {funpay_order_id}, игнор")
            return

        quantity = order_shortcut.amount
        if quantity is None:
            logger.error(f"[{user_id}] order {funpay_order_id}: amount=None — критическая ошибка, пропуск")
            return

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
            full_desc=full_desc,
        )
        db.add(order)
        db.flush()

        service = Service(
            order_id=order.order_id,
            status=ServiceStatus.PENDING_INPUT,
        )
        db.add(service)
        db.commit()

        with account_lock:
            chat_node = _find_chat_node(account, order_shortcut.buyer_username)

        if chat_node is None:
            logger.error(f"[{user_id}] Не найден chat node для {order_shortcut.buyer_username}, не можем написать")
            return

        order.chat_node = chat_node
        db.commit()

    with account_lock:
        _send(account, chat_node,
              f"Заказ #{funpay_order_id} оплачен ✅\n"
              f"💬 Возникнут вопросы — напишите /operator для связи с продавцом.\n\n"
              f"📎 Отправьте ссылку для выполнения заказа:")
    logger.info(f"[{user_id}] Новый заказ {funpay_order_id} создан, chat_node={chat_node}, ждём инпут от {order_shortcut.buyer_username}")


def _handle_new_message(
    event: NewMessageEvent,
    user_id: str,
    account: Account,
    account_lock: threading.Lock,
):
    msg = event.message

    logger.debug(
        f"[{user_id}] NewMessageEvent: author_id={msg.author_id} "
        f"by_bot={msg.by_bot} account_id={account.id} "
        f"chat_id={msg.chat_id} text={repr(msg.text)}"
    )

    if msg.by_bot or msg.author_id == account.id:
        logger.debug(f"[{user_id}] Сообщение отфильтровано: by_bot={msg.by_bot} is_self={msg.author_id == account.id}")
        return

    text = (msg.text or "").strip()
    buyer_id = msg.author_id
    chat_id = msg.chat_id

    # ── Глобальные команды (работают независимо от статуса заказа) ─────────────
    if text.startswith("/status "):
        with account_lock:
            _handle_status_command(text, buyer_id, chat_id, user_id, account)
        return

    if text == "/operator":
        logger.info(f"[{user_id}] /operator от buyer_id={buyer_id} chat_id={chat_id}")
        with account_lock:
            _handle_operator_command(buyer_id, chat_id, user_id, account)
        return

    with SyncSessionLocal() as db:
        # Ищем активный заказ: приоритет у PENDING_INPUT/AWAITING_CONFIRM (самый старый),
        # иначе — PROCESSING. Это корректно обрабатывает несколько заказов в одном чате.
        order = _get_pending_order(db, user_id, buyer_id)
        if not order:
            order = _get_processing_order(db, user_id, buyer_id)
        if not order:
            return

        service: Service = order.service
        if not service:
            return

        effective_chat_id = order.chat_node or chat_id

        # ── /change ───────────────────────────────────────────────────────────
        if text == "/change":
            if service.status in (ServiceStatus.PENDING_INPUT, ServiceStatus.AWAITING_CONFIRM):
                service.status = ServiceStatus.PENDING_INPUT
                order.buyer_input = None
                db.commit()
                with account_lock:
                    _send(account, effective_chat_id, "🔄 Отправьте новую ссылку:")
            return

        # ── pending_input: ждём ссылку/ник ───────────────────────────────────
        if service.status == ServiceStatus.PENDING_INPUT:
            if not text:
                return
            order.buyer_input = text
            service.status = ServiceStatus.AWAITING_CONFIRM
            db.commit()
            with account_lock:
                _send(account, effective_chat_id,
                      f"🔗 Ваша ссылка:\n↳ {text}\n\n"
                      f"⚠️ Перепроверьте ссылку внимательно.\nВыберите действие:\n"
                      f"• /next — ссылка верна → продолжить выполнение\n"
                      f"• /cancel — отмена заказа → средства будут возвращены\n"
                      f"• /change — изменить ссылку → указать новую")
            return

        # ── awaiting_confirm ──────────────────────────────────────────────────
        if service.status == ServiceStatus.AWAITING_CONFIRM:
            if text == "/next":
                _start_smm_order(order, service, db, account, account_lock, effective_chat_id, user_id)
            elif text == "/cancel":
                service.status = ServiceStatus.FAILED
                db.commit()
                with account_lock:
                    _send(account, effective_chat_id, "❌ Заказ отменён. Ожидайте возврат средств продавцом.")
            return


# ── SMM Order Flow ────────────────────────────────────────────────────────────

def _start_smm_order(
    order: Order,
    service: Service,
    db: Session,
    account: Account,
    account_lock: threading.Lock,
    chat_id: int,
    user_id: str,
):
    automation = _find_automation(db, user_id, order.full_desc or '')
    if not automation:
        with account_lock:
            _send(account, chat_id, "❌ Ошибка конфигурации автоматизации.")
        service.status = ServiceStatus.NEEDS_ATTENTION
        db.commit()
        return

    lot_hash: LotServiceHash = db.query(LotServiceHash).filter(
        LotServiceHash.lot_id == automation.lot_id
    ).first()
    if not lot_hash:
        with account_lock:
            _send(account, chat_id, "❌ Ошибка: SMM сервис не настроен для этого лота.")
        service.status = ServiceStatus.NEEDS_ATTENTION
        db.commit()
        return

    user: User = db.query(User).filter(User.user_id == user_id).first()
    if not user.smm_key:
        with account_lock:
            _send(account, chat_id, "❌ Ошибка: SMM ключ не настроен.")
        service.status = ServiceStatus.NEEDS_ATTENTION
        db.commit()
        return

    service.smm_service_id = lot_hash.smm_service_id
    service.service_name = lot_hash.service_name
    service_id_str = str(service.service_id)
    order_id_str = str(order.order_id)
    db.flush()

    try:
        smm_order_id = asyncio.run(smm_api.create_order(
            smm_key=user.smm_key,
            service_id=lot_hash.smm_service_id,
            link=order.buyer_input,
            quantity=order.quantity,
        ))
    except Exception as e:
        logger.error(f"SMM create_order failed: {e}")
        with account_lock:
            _send(account, chat_id, f"❌ Ошибка при создании SMM заказа: {e}\n"
                                    f"Напишите /operator для связи с продавцом.")
        # Используем свежую сессию — shared db может быть в неконсистентном состоянии
        with SyncSessionLocal() as fresh_db:
            svc = fresh_db.query(Service).filter(Service.service_id == service_id_str).first()
            if svc:
                svc.status = ServiceStatus.NEEDS_ATTENTION
                fresh_db.commit()
        return

    service.smm_order_id = smm_order_id
    service.status = ServiceStatus.PROCESSING
    funpay_order_id = order.funpay_order_id
    buyer_username = order.buyer_username or ""
    db.commit()

    with account_lock:
        _send(account, chat_id,
              f"✔️ Заказ #{funpay_order_id} сформирован и начал выполняться.\n"
              f"🔍 Проверить статус: /status #{funpay_order_id}")

    poll_thread = threading.Thread(
        target=_poll_smm_status,
        args=(str(service.service_id), funpay_order_id, user_id, chat_id, account, account_lock, buyer_username),
        daemon=True,
    )
    poll_thread.start()


def _poll_smm_status(
    service_id: str,
    funpay_order_id: str,
    user_id: str,
    chat_id: int,
    account: Account,
    account_lock: threading.Lock,
    buyer_username: str = "",
):
    """Поллинг статуса SMM заказа. Крутится пока не done/failed."""
    import time

    try:
        while True:
            time.sleep(settings.SMM_POLLING_INTERVAL)

            with SyncSessionLocal() as db:
                service: Service = db.query(Service).filter(
                    Service.service_id == service_id
                ).first()

                if not service or service.status in (
                    ServiceStatus.DONE, ServiceStatus.FAILED, ServiceStatus.NEEDS_ATTENTION
                ):
                    break

                user: User = db.query(User).filter(User.user_id == user_id).first()

                try:
                    data = asyncio.run(smm_api.get_status(user.smm_key, service.smm_order_id))
                except smm_api.SMMError as e:
                    logger.error(f"SMM status poll error: {e}")
                    continue

                smm_status = data.get("status", "")
                logger.info(f"SMM order {service.smm_order_id} status: {smm_status}")

                if smm_status == "Completed":
                    service.status = ServiceStatus.DONE
                    order: Order = service.order
                    order.status = FunPayOrderStatus.CLOSED
                    db.commit()
                    with account_lock:
                        _send(account, chat_id,
                              f"✔️ Заказ #{funpay_order_id} выполнен. Пожалуйста, зайдите в раздел «Покупки», "
                              f"выберите его в списке и нажмите «Подтвердить выполнение заказа».")
                        time.sleep(0.5)
                        _send(account, chat_id,
                              f"⭐️ {buyer_username}, просим вас оставить отзыв о качестве выполненной работы.")
                    break

                elif smm_status in ("Canceled", "Fail", "Partial"):
                    service.status = ServiceStatus.NEEDS_ATTENTION
                    db.commit()
                    with account_lock:
                        _send(account, chat_id,
                              f"⚠️ Заказ #{funpay_order_id} завершился со статусом: {smm_status}. "
                              f"Продавец разберётся и свяжется с вами.")
                    break

    except Exception as e:
        logger.error(f"Polling thread crashed: {e}", exc_info=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

# ── Status label helper ────────────────────────────────────────────────────────

_STATUS_LABEL_MAP = {
    ServiceStatus.PENDING_INPUT:       "Ожидание",
    ServiceStatus.AWAITING_CONFIRM:    "Ожидание",
    ServiceStatus.PROCESSING:          "Выполняется",
    ServiceStatus.DONE:                "Выполнен",
    ServiceStatus.FAILED:              "Отменен",
    ServiceStatus.NEEDS_ATTENTION:     "Ожидает решения",
    ServiceStatus.OPERATOR_REQUESTED:  "Оператор",
}


def _service_status_label(status) -> str:
    return _STATUS_LABEL_MAP.get(status, "—") if status else "—"


def _handle_status_command(
    text: str,
    buyer_id: int,
    chat_id: int,
    user_id: str,
    account: "Account",
):
    """Обрабатывает /status #ORDER123 — защищена от просмотра чужих заказов."""
    import re as _re
    m = _re.match(r"^/status\s+#?(\S+)$", text.strip())
    if not m:
        return
    order_id_str = m.group(1).strip()

    with SyncSessionLocal() as db:
        order: Order = (
            db.query(Order)
            .options(joinedload(Order.service))
            .filter(Order.funpay_order_id == order_id_str)
            .first()
        )
        if not order:
            _send(account, chat_id, f"❌ Заказ #{order_id_str} не найден.")
            return
        if order.buyer_id != buyer_id:
            _send(account, chat_id, f"❌ Заказ #{order_id_str} вам не принадлежит.")
            return

        effective_chat_id = order.chat_node or chat_id
        svc = order.service
        smm_id   = str(svc.smm_order_id) if svc and svc.smm_order_id else "—"
        date_str = svc.date.strftime("%Y-%m-%d %H:%M:%S") if svc and svc.date else "—"
        link     = order.buyer_input or "—"
        count    = str(order.quantity) if order.quantity else "—"
        status   = _service_status_label(svc.status if svc else None)

        _send(account, effective_chat_id,
              f"ID: {smm_id}\n"
              f"Date: {date_str}\n"
              f"Link: {link}\n"
              f"Count: {count}\n"
              f"Status: {status}")


def _handle_operator_command(
    buyer_id: int,
    chat_id: int,
    user_id: str,
    account: "Account",
):
    """Покупатель запрашивает продавца — статус → OPERATOR_REQUESTED."""
    logger.info(f"[{user_id}] _handle_operator_command: buyer_id={buyer_id} chat_id={chat_id}")
    with SyncSessionLocal() as db:
        # Диагностика: все заказы этого покупателя
        all_orders = (
            db.query(Order)
            .join(Service, Service.order_id == Order.order_id)
            .filter(Order.user_id == user_id, Order.buyer_id == buyer_id)
            .all()
        )
        for o in all_orders:
            logger.info(
                f"[{user_id}] Заказ покупателя: funpay_id={o.funpay_order_id} "
                f"order_status={o.status} service_status={o.service.status if o.service else 'NO_SERVICE'} "
                f"chat_node={o.chat_node}"
            )

        order: Order = (
            db.query(Order)
            .join(Service, Service.order_id == Order.order_id)
            .filter(
                Order.user_id == user_id,
                Order.buyer_id == buyer_id,
                Order.status == FunPayOrderStatus.PAID,
                Service.status.notin_([
                    ServiceStatus.DONE,
                    ServiceStatus.FAILED,
                    ServiceStatus.OPERATOR_REQUESTED,
                ]),
            )
            .order_by(Order.created_at.desc())
            .first()
        )
        if not order:
            logger.warning(f"[{user_id}] /operator: активный заказ не найден для buyer_id={buyer_id}")
            _send(account, chat_id, "❌ Нет активного заказа для обращения к продавцу.")
            return

        logger.info(f"[{user_id}] /operator: найден заказ {order.funpay_order_id} chat_node={order.chat_node}")
        funpay_order_id = order.funpay_order_id
        effective_chat_id = order.chat_node or chat_id
        order.service.status = ServiceStatus.OPERATOR_REQUESTED
        db.commit()
        logger.info(f"[{user_id}] /operator: статус → OPERATOR_REQUESTED, отправляем в chat_id={effective_chat_id}")

        # Получаем telegram_id продавца для уведомления
        seller: User = db.query(User).filter(User.user_id == user_id).first()
        seller_telegram_id = seller.telegram_id if seller else None

    _send(account, effective_chat_id,
          "✅ Запрос отправлен. Продавец увидит его в панели управления и свяжется с вами.")
    logger.info(f"[{user_id}] /operator: сообщение отправлено покупателю")

    # Уведомляем продавца в Telegram (вне db-сессии, не блокируем если упадёт)
    threading.Thread(
        target=_notify_telegram_operator,
        args=(seller_telegram_id, funpay_order_id),
        daemon=True,
    ).start()


def _send(account: Account, chat_id: int, text: str):
    try:
        account.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"send_message failed: chat_id={chat_id} error={e}", exc_info=True)


def _notify_telegram_operator(telegram_id: int, funpay_order_id: str):
    """Шлёт продавцу Telegram-сообщение о запросе оператора."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token or not telegram_id:
        logger.warning("TELEGRAM_BOT_TOKEN не задан или telegram_id пуст — уведомление не отправлено")
        return
    try:
        import requests as _req
        resp = _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": telegram_id, "text": f"Ордер #{funpay_order_id} запрошен оператор"},
            timeout=5,
        )
        if not resp.ok:
            logger.error(f"Telegram notify failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


def _find_chat_node(account: Account, buyer_username: str) -> int | None:
    try:
        saved = account._Account__saved_chats  # dict {node_id: ChatShortcut}
        for node_id, chat in saved.items():
            if chat.name == buyer_username:
                return node_id
    except Exception as e:
        logger.error(f"_find_chat_node error: {e}")
    return None


def _find_automation(db: Session, user_id: str, order_description: str) -> CurrentUserService | None:
    """
    Ищет автоматизацию по lot_id из описания ордера.
    Продавец обязан добавить 'id: <funpay_lot_id>' в описание лота.
    """
    import re
    match = re.search(r'id:\s*(\d+)', order_description, re.IGNORECASE)
    if not match:
        logger.warning(f"Не найден 'id: ...' в описании ордера: '{order_description}'")
        return None

    funpay_lot_id = match.group(1)

    return (
        db.query(CurrentUserService)
        .join(Lot, Lot.lot_id == CurrentUserService.lot_id)
        .filter(
            CurrentUserService.user_id == user_id,
            CurrentUserService.is_active == True,
            Lot.funpay_lot_id == funpay_lot_id,
        )
        .first()
    )


def _get_pending_order(db: Session, user_id: str, buyer_id: int) -> Order | None:
    """
    Возвращает самый старый заказ покупателя в статусе PENDING_INPUT или AWAITING_CONFIRM.
    Это гарантирует последовательную обработку при нескольких заказах в одном чате.
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
            ]),
        )
        .order_by(Order.created_at.asc())
        .first()
    )


def _get_processing_order(db: Session, user_id: str, buyer_id: int) -> Order | None:
    """Возвращает заказ в статусе PROCESSING (для /status команды)."""
    return (
        db.query(Order)
        .join(Service, Service.order_id == Order.order_id)
        .filter(
            Order.user_id == user_id,
            Order.buyer_id == buyer_id,
            Order.status == FunPayOrderStatus.PAID,
            Service.status == ServiceStatus.PROCESSING,
        )
        .order_by(Order.created_at.desc())
        .first()
    )