from supabase import Client, create_client

from .config import settings


def get_supabase() -> Client:
    """
    Cria e retorna o cliente Supabase.

    IMPORTANTE:
    SUPABASE_URL deve ser somente a URL base do projeto:
    
    https://SEU-PROJETO.supabase.co

    Não coloque /rest/v1 no final.
    """

    if not settings.supabase_url:
        raise RuntimeError(
            "SUPABASE_URL não configurada"
        )

    if not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY não configurada"
        )

    # Remove barras extras do final.
    # Também evita que uma URL configurada
    # incorretamente com /rest/v1 gere:
    # /rest/v1/rest/v1/...
    supabase_url = (
        settings.supabase_url
        .strip()
        .rstrip("/")
    )

    if supabase_url.endswith("/rest/v1"):
        supabase_url = supabase_url[
            :-len("/rest/v1")
        ].rstrip("/")

    return create_client(
        supabase_url,
        settings.supabase_service_role_key,
    )
