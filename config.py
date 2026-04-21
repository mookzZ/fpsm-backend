from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str = "changeme-in-production"
    SMM_POLLING_INTERVAL: int = 30  # seconds between SMM status checks
    TELEGRAM_BOT_TOKEN: str = ""  # токен бота для уведомлений продавцу

    class Config:
        env_file = ".env"


settings = Settings()
