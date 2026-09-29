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
from .dashboard import router as dashboard_router
from .client import router as client_router
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
        settings.supabase_secret_key,
    ]

    for secret in secrets:
        if secret:
            message = message.replace(
                secret,
                "[REDACTED]",
            )

    return message


# ==================================================
# NOTIFICAÇÕES PRIVADAS
# ==================================================

async def notify_group_admins(
    context: ContextTypes.DEFAULT_TYPE,
    chat,
    message: str,
):
    """
    Envia notificações de moderação em mensagem privada
    para os administradores do grupo.

    O SentinelChat não publica mais os alertas de moderação
    dentro do grupo. O administrador precisa ter iniciado
    o bot em conversa privada pelo menos uma vez para que
    o Telegram permita o envio da mensagem.
    """

    try:
        administrators = await context.bot.get_chat_administrators(
            chat.id
        )
    except Exception as exc:
        logger.warning(
            "Não foi possível obter os administradores do grupo %s: %s",
            chat.id,
            redact_error(exc),
        )
        return

    for administrator in administrators:
        admin_user = administrator.user

        try:
            await context.bot.send_message(
                chat_id=admin_user.id,
                text=message,
            )

            logger.info(
                "Notificação privada enviada para admin %s do grupo %s",
                admin_user.id,
                chat.id,
            )

        except Exception as exc:
            # O Telegram impede o bot de iniciar uma conversa
            # privada com um usuário que nunca abriu o bot.
            logger.warning(
                "Não foi possível enviar notificação privada "
                "para admin %s do grupo %s: %s",
                admin_user.id,
                chat.id,
                redact_error(exc),
            )


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
# CONTROLE DE MODO DE MODERAÇÃO
# ==================================================

async def is_group_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Verifica se quem executou o comando é administrador do grupo."""

    if not update.effective_user or not update.effective_chat:
        return False

    if update.effective_chat.type not in ("group", "supergroup"):
        return False

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as exc:
        logger.warning(
            "Não foi possível validar administrador: %s",
            redact_error(exc),
        )
        return False


async def set_moderation_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
):
    """
    Altera o modo de moderação somente para administradores.

    Modos:
    - dryrun: apenas detecta e notifica.
    - restrict: restringe membros que violarem as regras.
    - ban: bane membros que violarem as regras.
    """

    if not update.message or not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "⚠️ Este comando precisa ser executado dentro do grupo."
        )
        return

    if not await is_group_admin(update, context):
        await update.message.reply_text(
            "⛔ Apenas administradores do grupo podem alterar o modo."
        )
        return

    try:
        chat_id, _ = ensure_chat_registered(chat)
        db = get_supabase()

        if mode == "dryrun":
            action = "review"
            dry_run = True
            label = "DRY-RUN"
            explanation = (
                "Nenhuma punição será aplicada. "
                "O SentinelChat apenas detectará e notificará."
            )

        elif mode == "restrict":
            action = "restrict"
            dry_run = False
            label = "RESTRIÇÃO"
            explanation = (
                "Membros que violarem as regras serão restringidos."
            )

        elif mode == "ban":
            action = "ban"
            dry_run = False
            label = "BAN"
            explanation = (
                "Membros que violarem as regras serão banidos."
            )

        else:
            await update.message.reply_text(
                "⚠️ Modo inválido."
            )
            return

        (
            db.table("moderation_rules")
            .update(
                {
                    "action": action,
                    "dry_run": dry_run,
                }
            )
            .eq("chat_id", chat_id)
            .execute()
        )

        await update.message.reply_text(
            "🛡️ SentinelChat — modo atualizado\n\n"
            f"🔎 Modo: {label}\n"
            f"📋 Ação: {action}\n\n"
            f"ℹ️ {explanation}"
        )

        logger.info(
            "Modo de moderação alterado no grupo %s para %s",
            chat.id,
            label,
        )

    except Exception as exc:
        logger.exception(
            "Erro alterando modo de moderação"
        )

        await update.message.reply_text(
            "⚠️ Não foi possível alterar o modo de moderação.\n\n"
            f"🔎 Diagnóstico: {redact_error(exc)}"
        )


async def mode_dryrun(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await set_moderation_mode(
        update,
        context,
        "dryrun",
    )


async def mode_restrict(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await set_moderation_mode(
        update,
        context,
        "restrict",
    )


async def mode_ban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await set_moderation_mode(
        update,
        context,
        "ban",
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
            # NOTIFICAÇÃO DE APROVAÇÃO
            # ------------------------------------------

            if not violations:
                await notify_group_admins(
                    context,
                    chat,
                    (
                        "🟢 SentinelChat — análise concluída\n\n"
                        f"👤 Usuário: {user.full_name}\n"
                        f"🆔 ID: {user.id}\n\n"
                        "✅ Foto de perfil\n"
                        "✅ Primeiro nome\n"
                        "✅ Sobrenome\n"
                        "✅ Username\n\n"
                        "🛡️ Nenhuma violação encontrada."
                    ),
                )

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

                notification = (
                    "🔎 SentinelChat — possível violação\n\n"
                    f"👤 Usuário: {user.full_name}\n"
                    f"🆔 ID: {user.id}\n"
                    f"⚠️ Motivos: {', '.join(violations)}\n\n"
                )

                if rules.dry_run:
                    notification += (
                        "🧪 DRY-RUN ativo.\n"
                        "Nenhuma ação foi aplicada."
                    )
                else:
                    notification += (
                        f"🛡️ Ação aplicada: {action_taken or 'review'}."
                    )

                await notify_group_admins(
                    context,
                    chat,
                    notification,
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
    CommandHandler(
        "modo_dryrun",
        mode_dryrun,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "modo_restrict",
        mode_restrict,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "modo_ban",
        mode_ban,
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

    # ==================================================
    # DIAGNÓSTICO SEGURO DO BOT
    # ==================================================
    #
    # Confirma qual bot pertence ao token configurado
    # no Render sem jamais registrar o token.
    #

    try:

        bot_info = await telegram_app.bot.get_me()

        logger.info(
            "========================================"
        )

        logger.info(
            "BOT TELEGRAM: @%s",
            bot_info.username,
        )

        logger.info(
            "BOT ID TELEGRAM: %s",
            bot_info.id,
        )

        logger.info(
            "BOT NOME: %s",
            bot_info.first_name,
        )

        logger.info(
            "========================================"
        )

    except Exception:

        logger.exception(
            "Falha ao identificar o bot Telegram"
        )

    # ==================================================
    # WEBHOOK
    # ==================================================

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

        # Registrar o webhook diretamente.
        # Não apagar o webhook anterior durante o startup.
        # Isso evita uma janela sem webhook em reinicializações.
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

    # IMPORTANTE:
    # Não remover o webhook durante shutdown/restart.
    # O Render pode reiniciar o processo e o Telegram deve
    # continuar apontando para este endpoint.
    try:

        logger.info(
            "Encerrando SentinelChat sem remover o webhook."
        )

        await telegram_app.stop()

    finally:

        await telegram_app.shutdown()


# ==================================================
# FASTAPI
# ==================================================

api = FastAPI(
    title="SentinelChat",
    version="0.5.0",
    description=(
        "Telegram community protection "
        "and moderation platform."
    ),
    lifespan=lifespan,
)

# Painel web de configuração protegido por DASHBOARD_TOKEN.
api.include_router(dashboard_router)
api.include_router(client_router)


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
        "version": "0.5.0",
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
