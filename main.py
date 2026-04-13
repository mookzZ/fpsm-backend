import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import async_engine, Base
from routers import auth, lots, orders, automations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаём таблицы (в проде лучше Alembic)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB tables ready")

    # Поднимаем воркеры для всех активных юзеров
    from services.funpay_worker import worker_manager
    worker_manager.start_all_active()
    logger.info("FunPay workers started")

    yield

    logger.info("Shutting down")


app = FastAPI(title="FunPay Automation API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fpsm-frontend.vercel.app"],  # в проде указать конкретный Vercel домен
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
