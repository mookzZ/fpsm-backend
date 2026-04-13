import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import async_engine, Base
from routers import auth, lots, orders, automations

# ── Logging ───────────────────────────────────────────────────────────────────

log_formatter = logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")

file_handler = RotatingFileHandler(
    "app.log",
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Заглушаем шумные либы
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)

logger = logging.getLogger("main")


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB tables ready")

    from services.funpay_worker import worker_manager
    worker_manager.start_all_active()
    logger.info("FunPay workers started")

    yield

    logger.info("Shutting down")


app = FastAPI(title="FunPay Automation API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fpsm-frontend.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(lots.router)
app.include_router(orders.router)
app.include_router(automations.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/logs")
async def get_logs(lines: int = 200):
    """Последние N строк лога — только для дебага, убери в проде."""
    try:
        with open("app.log", "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return {"lines": all_lines[-lines:]}
    except FileNotFoundError:
        return {"lines": []}
