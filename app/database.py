from supabase import Client, create_client

from .config import settings


def get_supabase() -> Client:
    if not settings.supabase_url:
        raise RuntimeError("SUPABASE_URL não configurada")

    if not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY não configurada"
        )

    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )