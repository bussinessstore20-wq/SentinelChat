from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from pydantic import BaseModel
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings
from .database import get_supabase
from .moderation import (
    MemberProfile,
    ModerationRule,
    analyze_member,
)


# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("sentinelchat")


# ==================================================
# TELEGRAM
# ==================================================

telegram_app = (
    Application.builder()
    .token(settings.telegram_bot_token)
    .build()
)


# ==================================================
# HELPERS
# ==================================================

def redact_error(exc):
    """
    Remove tokens/chaves de mensagens de erro.
    """

    message = f"{type(exc).__name__}: {exc}"

    secrets = [
        settings.telegram_bot_token,
        settings.supabase_service_role_key,
    ]

    for secret in secrets:
        if secret:
            message = message.replace(
                secret,
                "[REDACTED]",
            )

    return message


# ==================================================
# SUPABASE
# ==================================================

def ensure_chat_registered(chat):

    db = get_supabase()

    # --------------------------------------------------
    # WORKSPACE
    # --------------------------------------------------

    try:

        result = (
            db.table("workspaces")
            .select("id")
            .eq(
                "name",
                "SentinelChat",
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise RuntimeError(
            "SUPABASE_WORKSPACES_SELECT: "
            + redact_error(exc)
        ) from exc

    if result.data:

        workspace_id = result.data[0]["id"]

    else:

        try:

            result = (
                db.table("workspaces")
                .insert(
                    {
                        "name": "SentinelChat",
                    }
                )
                .execute()
            )

        except Exception as exc:

            raise RuntimeError(
                "SUPABASE_WORKSPACES_INSERT: "
                + redact_error(exc)
            ) from exc

        if not result.data:

            raise RuntimeError(
                "SUPABASE_WORKSPACES_INSERT: "
                "nenhum registro retornado"
            )

        workspace_id = result.data[0]["id"]

    # --------------------------------------------------
    # TELEGRAM CHAT
    # --------------------------------------------------

    try:

        result = (
            db.table("telegram_chats")
            .select("id")
            .eq(
                "workspace_id",
                workspace_id,
            )
            .eq(
                "telegram_chat_id",
                chat.id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise RuntimeError(
            "SUPABASE_TELEGRAM_CHATS_SELECT: "
            + redact_error(exc)
        ) from exc

    if result.data:

        chat_id = result.data[0]["id"]

    else:

        try:

            result = (
                db.table("telegram_chats")
                .insert(
                    {
                        "workspace_id": workspace_id,
                        "telegram_chat_id": chat.id,
                        "title": chat.title or "Sem título",
                        "chat_type": chat.type,
                        "is_active": True,
                    }
                )
                .execute()
            )

        except Exception as exc:

            raise RuntimeError(
                "SUPABASE_TELEGRAM_CHATS_INSERT: "
                + redact_error(exc)
            ) from exc

        if not result.data:

            raise RuntimeError(
                "SUPABASE_TELEGRAM_CHATS_INSERT: "
                "nenhum registro retornado"
            )

        chat_id = result.data[0]["id"]

    # --------------------------------------------------
    # REGRAS
    # --------------------------------------------------

    try:

        result = (
            db.table("moderation_rules")
            .select(
                "require_photo,"
                "require_first_name,"
                "require_last_name,"
                "require_username,"
                "ignore_admins,"
                "action,"
                "dry_run"
            )
            .eq(
                "chat_id",
                chat_id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise RuntimeError(
            "SUPABASE_MODERATION_RULES_SELECT: "
            + redact_error(exc)
        ) from exc

    if result.data:

        row = result.data[0]

    else:

        row = {
            "require_photo": True,
            "require_first_name": True,
            "require_last_name": True,
            "require_username": True,
            "ignore_admins": True,
            "action": "review",
            "dry_run": True,
        }

        try:

            (
                db.table("moderation_rules")
                .insert(
                    {
                        "chat_id": chat_id,
                        **row,
                    }
                )
                .execute()
            )

        except Exception as exc:

            raise RuntimeError(
                "SUPABASE_MODERATION_RULES_INSERT: "
                + redact_error(exc)
            ) from exc

    try:

        rules = ModerationRule(**row)

    except Exception as exc:

        raise RuntimeError(
            "MODERATION_RULE_PARSE: "
            + redact_error(exc)
        ) from exc

    return chat_id, rules


# ==================================================
# /START
# ==================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "🛡️ SentinelChat ativo.\n\n"
        "Proteção e moderação da sua comunidade.\n\n"
        "Adicione este bot como administrador "
        "de um grupo para ativar a proteção."
    )


# ==================================================
# /STATUS
# ==================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "⚠️ O /status precisa ser executado "
            "dentro de um grupo ou supergrupo."
        )

        return

    try:

        chat_id, rules = ensure_chat_registered(
            chat
        )

        db = get_supabase()

        scans = (
            db.table("member_scans")
            .select(
                "id",
                count="exact",
            )
            .eq(
                "chat_id",
                chat_id,
            )
            .execute()
        )

        events = (
            db.table("moderation_events")
            .select(
                "id",
                count="exact",
            )
            .eq(
                "chat_id",
                chat_id,
            )
            .execute()
        )

        mode = (
            "DRY-RUN — nenhuma punição"
            if rules.dry_run
            else rules.action.upper()
        )

        await update.message.reply_text(
            "🛡️ SentinelChat — Status\n\n"
            "🟢 Bot: online\n"
            "🟢 Grupo: conectado\n"
            f"🔎 Modo: {mode}\n"
            f"👤 Análises: {scans.count or 0}\n"
            f"⚠️ Eventos: {events.count or 0}\n\n"
            "Regras ativas:\n"
            f"{'✅' if rules.require_photo else '⬜'} "
            "Foto de perfil\n"
            f"{'✅' if rules.require_first_name else '⬜'} "
            "Primeiro nome\n"
            f"{'✅' if rules.require_last_name else '⬜'} "
            "Sobrenome\n"
            f"{'✅' if rules.require_username else '⬜'} "
            "Username\n"
            f"{'✅' if rules.ignore_admins else '⬜'} "
            "Ignorar administradores"
        )

    except Exception as exc:

        logger.exception(
            "Erro no comando /status"
        )

        await update.message.reply_text(
            "⚠️ SentinelChat está online, "
            "mas ocorreu um erro ao consultar o grupo.\n\n"
            "🔎 Diagnóstico:\n"
            f"{redact_error(exc)}"
        )


# ==================================================
# NOVOS MEMBROS
# ==================================================

async def handle_new_members(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not update.message.new_chat_members:
        return

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in (
        "group",
        "supergroup",
    ):

        return

    chat_id = None

    try:

        chat_id, rules = ensure_chat_registered(
            chat
        )

        db = get_supabase()

        for user in update.message.new_chat_members:

            member = (
                await context.bot.get_chat_member(
                    chat.id,
                    user.id,
                )
            )

            is_admin = member.status in (
                "administrator",
                "creator",
            )

            photos = (
                await context.bot.get_user_profile_photos(
                    user.id,
                    limit=1,
                )
            )

            has_photo = bool(
                photos.total_count
            )

            profile = MemberProfile(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                has_photo=has_photo,
                is_admin=is_admin,
            )

            violations = analyze_member(
                profile,
                rules,
            )

            action_taken = (
                "review"
                if violations
                else None
            )

            # ------------------------------------------
            # AÇÃO DE MODERAÇÃO
            # ------------------------------------------

            if (
                violations
                and not rules.dry_run
                and not is_admin
            ):

                if rules.action == "restrict":

                    await context.bot.restrict_chat_member(
                        chat.id,
                        user.id,
                        permissions=ChatPermissions(
                            can_send_messages=False,
                            can_send_audios=False,
                            can_send_documents=False,
                            can_send_photos=False,
                            can_send_videos=False,
                            can_send_video_notes=False,
                            can_send_voice_notes=False,
                            can_send_polls=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False,
                            can_change_info=False,
                            can_invite_users=False,
                            can_pin_messages=False,
                        ),
                    )

                    action_taken = "restrict"

                elif rules.action == "ban":

                    await context.bot.ban_chat_member(
                        chat.id,
                        user.id,
                    )

                    action_taken = "ban"

            # ------------------------------------------
            # SCAN
            # ------------------------------------------

            try:

                (
                    db.table("member_scans")
                    .insert(
                        {
                            "chat_id": chat_id,
                            "telegram_user_id": user.id,
                            "username": user.username,
                            "first_name": user.first_name,
                            "last_name": user.last_name,
                            "has_photo": has_photo,
                            "is_admin": is_admin,
                            "violations": violations,
                            "action_taken": action_taken,
                        }
                    )
                    .execute()
                )

            except Exception as exc:

                raise RuntimeError(
                    "SUPABASE_MEMBER_SCANS_INSERT: "
                    + redact_error(exc)
                ) from exc

            # ------------------------------------------
            # EVENTO
            # ------------------------------------------

            if violations:

                try:

                    (
                        db.table("moderation_events")
                        .insert(
                            {
                                "chat_id": chat_id,
                                "telegram_user_id": user.id,
                                "event_type": "member_violation",
                                "details": {
                                    "violations": violations,
                                    "dry_run": rules.dry_run,
                                    "action": action_taken,
                                },
                            }
                        )
                        .execute()
                    )

                except Exception as exc:

                    raise RuntimeError(
                        "SUPABASE_MODERATION_EVENTS_INSERT: "
                        + redact_error(exc)
                    ) from exc

                if rules.dry_run:

                    await update.message.reply_text(
                        "🔎 SentinelChat detectou "
                        "uma possível violação.\n\n"
                        f"👤 Usuário: {user.full_name}\n"
                        f"🆔 ID: {user.id}\n"
                        f"⚠️ Motivos: "
                        f"{', '.join(violations)}\n\n"
                        "🧪 DRY-RUN ativo.\n"
                        "Nenhuma ação foi aplicada."
                    )

    except Exception as exc:

        logger.exception(
            "Erro processando novo membro"
        )

        if chat_id:

            try:

                (
                    get_supabase()
                    .table("moderation_events")
                    .insert(
                        {
                            "chat_id": chat_id,
                            "event_type": "moderation_error",
                            "details": {
                                "error": type(exc).__name__,
                                "message": redact_error(exc),
                            },
                        }
                    )
                    .execute()
                )

            except Exception:

                logger.exception(
                    "Falha ao registrar erro"
                )


# ==================================================
# HANDLERS
# ==================================================

telegram_app.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "status",
        status,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_members,
    )
)


# ==================================================
# WEBHOOK
# ==================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    await telegram_app.initialize()

    await telegram_app.start()

    webhook_url = (
        settings.telegram_webhook_url
        .strip()
    )

    if not webhook_url:

        logger.error(
            "TELEGRAM_WEBHOOK_URL não configurada"
        )

        await telegram_app.stop()
        await telegram_app.shutdown()

        raise RuntimeError(
            "TELEGRAM_WEBHOOK_URL não configurada"
        )

    full_webhook_url = (
        webhook_url.rstrip("/")
        + settings.telegram_webhook_path
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "SentinelChat iniciando em WEBHOOK"
    )

    logger.info(
        "Webhook: %s",
        full_webhook_url,
    )

    logger.info(
        "========================================"
    )

    try:

        # Remove webhook anterior
        await telegram_app.bot.delete_webhook(
            drop_pending_updates=True
        )

        logger.info(
            "Webhook anterior removido."
        )

        # Cria webhook novo
        await telegram_app.bot.set_webhook(
            url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )

        # Confirma com o Telegram
        info = (
            await telegram_app.bot.get_webhook_info()
        )

        logger.info(
            "Webhook Telegram confirmado."
        )

        logger.info(
            "URL: %s",
            info.url,
        )

        logger.info(
            "Pending: %s",
            info.pending_update_count,
        )

        logger.info(
            "Último erro: %s",
            info.last_error_message,
        )

        logger.info(
            "Data último erro: %s",
            info.last_error_date,
        )

    except Exception:

        logger.exception(
            "Falha ao configurar webhook"
        )

        await telegram_app.stop()
        await telegram_app.shutdown()

        raise

    yield

    try:

        await telegram_app.bot.delete_webhook()

    finally:

        await telegram_app.stop()

        await telegram_app.shutdown()


# ==================================================
# FASTAPI
# ==================================================

api = FastAPI(
    title="SentinelChat",
    version="0.3.3",
    description=(
        "Telegram community protection "
        "and moderation platform."
    ),
    lifespan=lifespan,
)


# ==================================================
# HEALTH
# ==================================================

class HealthResponse(BaseModel):

    status: str
    environment: str
    telegram_mode: str


@api.get("/")
async def root():

    return {
        "name": "SentinelChat",
        "status": "online",
        "version": "0.3.3",
        "telegram_mode": settings.telegram_mode,
        "features": [
            "telegram",
            "webhook",
            "moderation",
            "supabase",
            "dry_run",
        ],
    }


@api.get(
    "/health",
    response_model=HealthResponse,
)
async def health():

    return HealthResponse(
        status="ok",
        environment=settings.app_env,
        telegram_mode=settings.telegram_mode,
    )


# ==================================================
# DIAGNÓSTICO DO WEBHOOK
# ==================================================

@api.get(
    "/telegram/webhook"
)
async def webhook_diagnostic():

    try:

        info = (
            await telegram_app.bot.get_webhook_info()
        )

        configured_url = (
            settings.telegram_webhook_url.rstrip("/")
            + settings.telegram_webhook_path
        )

        return {
            "ok": True,
            "telegram_mode": settings.telegram_mode,
            "configured_url": configured_url,
            "telegram_url": info.url,
            "pending_update_count": (
                info.pending_update_count
            ),
            "last_error_message": (
                info.last_error_message
            ),
            "last_error_date": (
                info.last_error_date
            ),
            "max_connections": (
                info.max_connections
            ),
        }

    except Exception as exc:

        logger.exception(
            "Erro consultando webhook"
        )

        return {
            "ok": False,
            "error": redact_error(exc),
        }


# ==================================================
# ENDPOINT DO TELEGRAM
# ==================================================

@api.post(
    "/telegram/webhook"
)
async def telegram_webhook(
    request: Request,
):

    data = await request.json()

    update_id = data.get(
        "update_id"
    )

    logger.info(
        "Telegram webhook recebido: "
        "update_id=%s",
        update_id,
    )

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    await telegram_app.process_update(
        update
    )

    logger.info(
        "Telegram webhook processado: "
        "update_id=%s",
        update_id,
    )

    return {
        "ok": True,
    }
