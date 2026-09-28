from supabase import Client, create_client

from .config import settings


def get_supabase() -> Client:
    """
    Cria o cliente administrativo do Supabase.

    A SUPABASE_SECRET_KEY é a chave preferencial para o backend.
    SUPABASE_SERVICE_ROLE_KEY permanece como fallback temporário.

    A SUPABASE_URL deve ser somente:
    https://SEU-PROJETO.supabase.co
    """

    if not settings.supabase_url:
        raise RuntimeError(
            "SUPABASE_URL não configurada"
        )

    # Preferir a nova Secret Key.
    # A chave antiga fica como fallback durante a migração.
    supabase_key = (
        settings.supabase_secret_key.strip()
        or settings.supabase_service_role_key.strip()
    )

    if not supabase_key:
        raise RuntimeError(
            "SUPABASE_SECRET_KEY não configurada "
            "(ou SUPABASE_SERVICE_ROLE_KEY legado)"
        )

    # Remove barras extras.
    supabase_url = (
        settings.supabase_url
        .strip()
        .rstrip("/")
    )

    # Corrige automaticamente uma configuração antiga
    # que tenha /rest/v1 no final.
    if supabase_url.endswith("/rest/v1"):
        supabase_url = supabase_url[
            :-len("/rest/v1")
        ].rstrip("/")

    # Cliente exclusivamente server-side.
    # Não utiliza nem persiste sessão de usuário.
    return create_client(
        supabase_url,
        supabase_key,
        options={
            "auto_refresh_token": False,
            "persist_session": False,
        },
    )
