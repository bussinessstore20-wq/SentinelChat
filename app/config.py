from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str

    # Webhook é o modo recomendado para o SentinelChat no Render.
    telegram_mode: str = "webhook"
    telegram_webhook_url: str = ""
    telegram_webhook_path: str = "/telegram/webhook"

    supabase_url: str = ""

    # Chave privada usada exclusivamente pelo backend.
    supabase_secret_key: str = ""
    supabase_service_role_key: str = ""

    # Chave publicável usada somente para inicializar o Supabase Auth no navegador.
    # Nunca usar a Secret Key no frontend.
    supabase_publishable_key: str = ""

    # Token privado do painel ADM legado.
    dashboard_token: str = ""

    app_env: str = "production"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
