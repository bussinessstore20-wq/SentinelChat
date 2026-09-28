from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str

    # Webhook é o modo recomendado para o SentinelChat no Render.
    telegram_mode: str = "webhook"

    # URL pública do serviço no Render.
    telegram_webhook_url: str = ""

    # Caminho utilizado pelo Telegram para entregar os updates.
    telegram_webhook_path: str = "/telegram/webhook"

    supabase_url: str = ""
    supabase_service_role_key: str = ""

    app_env: str = "production"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()