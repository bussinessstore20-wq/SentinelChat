from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel
from telegram import Update
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


telegram_app = (
    Application.builder()
    .token(settings.telegram_bot_token)
    .build()
)


def ensure_chat_registered(chat) -> tuple[str, ModerationRule]:
    """
    Garante que o grupo exista no Supabase
    e tenha uma configuração de moderação.
    """

    db = get_supabase()

    workspace_result = (
        db.table("workspaces")
        .select("id")
        .eq("name", "SentinelChat")
        .limit(1)
        .execute()
    )

    if workspace_result.data:
        workspace_id = workspace_result.data[0]["id"]
    else:
        workspace_result = (
            db.table("workspaces")
            .insert({"name": "SentinelChat"})
            .execute()
        )

        workspace_id = workspace_result.data[0]["id"]

    chat_result = (
        db.table("telegram_chats")
        .select("id")
        .eq("workspace_id", workspace_id)
        .eq("telegram_chat_id", chat.id)
        .limit(1)
        .execute()
    )

    if chat_result.data:
        chat_id = chat_result.data[0]["id"]
    else:
        chat_insert = (
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

        chat_id = chat_insert.data[0]["id"]

    rule_result = (
        db.table("moderation_rules")
        .select(
            "require_photo, "
            "require_first_name, "
            "require_last_name, "
            "require_username, "
            "ignore_admins, "
            "action, "
            "dry_run"
        )
        .eq("chat_id", chat_id)
        .limit(1)
        .execute()
    )

    if rule_result.data:
        row = rule_result.data[0]
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

        db.table("moderation_rules").insert(
            {
                "chat_id": chat_id,
                **row,
            }
        ).execute()

    return chat_id, ModerationRule(**row)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "🛡️ SentinelChat ativo.\n\n"
        "Proteção e moderação da sua comunidade.\n\n"
        "Adicione este bot como administrador de um grupo "
        "para ativar a proteção."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    try:
        chat = update.effective_chat

        if chat and chat.type in ("group", "supergroup"):
            chat_id, rules = ensure_chat_registered(chat)

            db = get_supabase()

            scans = (
                db.table("member_scans")
                .select("id", count="exact")
                .eq("chat_id", chat_id)
                .execute()
            )

            events = (
                db.table("moderation_events")
                .select("id", count="exact")
                .eq("chat_id", chat_id)
                .execute()
            )

            mode = (
                "DRY-RUN (nenhuma punição)"
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

            return

        await update.message.reply_text(
            "✅ SentinelChat online.\n\n"
            "Adicione o bot a um grupo para iniciar "
            "a proteção."
        )

    except Exception as exc:
        await update.message.reply_text(
            "⚠️ SentinelChat está online, mas ocorreu "
            "um erro ao consultar o grupo.\n\n"
            f"Erro: {type(exc).__name__}"
        )


async def handle_new_members(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Analisa novos membros que entram no grupo.
    """

    if not update.message:
        return

    if not update.message.new_chat_members:
        return

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    chat_id = None

    try:
        chat_id, rules = ensure_chat_registered(chat)

        db = get_supabase()

        for user in update.message.new_chat_members:

            member = await context.bot.get_chat_member(
                chat.id,
                user.id,
            )

            is_admin = member.status in (
                "administrator",
                "creator",
            )

            photos = await context.bot.get_user_profile_photos(
                user.id,
                limit=1,
            )

            has_photo = bool(photos.total_count)

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

            action_taken = None

            if violations:

                action_taken = "review"

                if not rules.dry_run and not is_admin:

                    if rules.action == "restrict":

                        await context.bot.restrict_chat_member(
                            chat.id,
                            user.id,
                            permissions={
                                "can_send_messages": False,
                                "can_send_audios": False,
                                "can_send_documents": False,
                                "can_send_photos": False,
                                "can_send_videos": False,
                                "can_send_video_notes": False,
                                "can_send_voice_notes": False,
                                "can_send_polls": False,
                                "can_send_other_messages": False,
                                "can_add_web_page_previews": False,
                                "can_change_info": False,
                                "can_invite_users": False,
                                "can_pin_messages": False,
                            },
                        )

                        action_taken = "restrict"

                    elif rules.action == "ban":

                        await context.bot.ban_chat_member(
                            chat.id,
                            user.id,
                        )

                        action_taken = "ban"

            db.table("member_scans").insert(
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
            ).execute()

            if violations:

                db.table("moderation_events").insert(
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
                ).execute()

                if rules.dry_run:

                    reasons = ", ".join(violations)

                    await update.message.reply_text(
                        "🔎 SentinelChat detectou uma "
                        "possível violação.\n\n"
                        f"👤 Usuário: {user.full_name}\n"
                        f"🆔 ID: {user.id}\n"
                        f"⚠️ Motivos: {reasons}\n\n"
                        "🧪 DRY-RUN ativo.\n"
                        "Nenhuma ação foi aplicada."
                    )

    except Exception as exc:

        try:
            db = get_supabase()

            db.table("moderation_events").insert(
                {
                    "chat_id": chat_id,
                    "event_type": "moderation_error",
                    "details": {
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                }
            ).execute()

        except Exception:
            pass


# ==========================================
# HANDLERS DO TELEGRAM
# ==========================================

telegram_app.add_handler(
    CommandHandler("start", start)
)

telegram_app.add_handler(
    CommandHandler("status", status)
)

telegram_app.add_handler(
    MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_members,
    )
)


# ==========================================
# FASTAPI / WEBHOOK
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    await telegram_app.initialize()
    await telegram_app.start()

    # IMPORTANTE:
    # Não usamos start_polling().
    #
    # O Telegram enviará os updates diretamente
    # para /telegram/webhook.

    webhook_url = settings.telegram_webhook_url.strip()

    if webhook_url:

        full_webhook_url = (
            webhook_url.rstrip("/")
            + settings.telegram_webhook_path
        )

        await telegram_app.bot.set_webhook(
            url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

    yield

    await telegram_app.bot.delete_webhook()

    await telegram_app.stop()
    await telegram_app.shutdown()


api = FastAPI(
    title="SentinelChat",
    version="0.3.0",
    description=(
        "Telegram community protection "
        "and moderation platform."
    ),
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    status: str
    environment: str
    telegram_mode: str


@api.get("/")
async def root():
    return {
        "name": "SentinelChat",
        "status": "online",
        "version": "0.3.0",
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


@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    await telegram_app.process_update(update)

    return {
        "ok": True
    }