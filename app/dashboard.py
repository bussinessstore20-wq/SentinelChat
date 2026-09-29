from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import httpx

from .config import settings
from .database import get_supabase

router = APIRouter()

def current_user(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sessão não autenticada.")
    access_token = authorization.split(" ", 1)[1].strip()
    try:
        user_response = get_supabase().auth.get_user(access_token)
        user = getattr(user_response, "user", None)
        if not user:
            raise ValueError("invalid session")
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Sessão expirada. Faça login novamente.")

def check_token(authorization: str | None):
    # Compatibilidade temporária: endpoints antigos passam a exigir Supabase Auth.
    return current_user(authorization)

def require_admin(authorization: str | None):
    user = current_user(authorization)
    rows = get_supabase().table("profiles").select("role,status").eq("id", str(user.id)).limit(1).execute().data or []
    if not rows or rows[0].get("role") != "admin" or rows[0].get("status") != "active":
        raise HTTPException(403, "Acesso restrito ao administrador.")
    return user

@router.get("/api/auth/public-config")
async def auth_public_config():
    return {"url": settings.supabase_url.rstrip("/"), "publishable_key": settings.supabase_publishable_key}

@router.post("/api/auth/login")
async def auth_login(payload: dict):
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    if not email or not password:
        raise HTTPException(400, "Informe e-mail e senha.")
    try:
        result = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
        session = getattr(result, "session", None)
        user = getattr(result, "user", None)
        if not session or not user:
            raise ValueError("login failed")
        profile = get_supabase().table("profiles").select("full_name,role,status").eq("id", str(user.id)).limit(1).execute().data or []
        if profile and profile[0].get("status") != "active":
            raise HTTPException(403, "Conta suspensa ou inativa.")
        return {"ok": True, "access_token": session.access_token, "user": {"id": str(user.id), "email": user.email, **(profile[0] if profile else {})}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, "E-mail ou senha inválidos.")

@router.post("/api/auth/signup")
async def auth_signup(payload: dict):
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    full_name = str(payload.get("full_name") or "").strip()
    if not email or len(password) < 8:
        raise HTTPException(400, "Informe um e-mail e uma senha com pelo menos 8 caracteres.")
    if not full_name:
        raise HTTPException(400, "Informe seu nome completo.")
    try:
        db = get_supabase()
        existing_admins = db.table("profiles").select("id").eq("role", "admin").limit(1).execute().data or []
        result = db.auth.sign_up({"email": email, "password": password, "options": {"data": {"full_name": full_name}}})
        user = getattr(result, "user", None)
        if not user:
            raise ValueError("signup failed")
        is_admin = (bool(settings.saas_admin_email.strip()) and email == settings.saas_admin_email.strip().lower()) or not existing_admins
        db.table("profiles").update({"full_name": full_name, "role": "admin" if is_admin else "customer", "status": "active"}).eq("id", str(user.id)).execute()
        if is_admin:
            legacy = db.table("workspaces").select("id,owner_id").is_("owner_id", "null").limit(1).execute().data or []
            if legacy:
                workspace = db.table("workspaces").update({"owner_id": str(user.id), "name": legacy[0].get("id") and "SentinelChat — Administração"}).eq("id", legacy[0]["id"]).execute().data
            else:
                workspace = db.table("workspaces").insert({"name": f"{full_name or email.split('@')[0]} — Workspace", "owner_id": str(user.id)}).execute().data
        else:
            workspace = db.table("workspaces").insert({"name": f"{full_name or email.split('@')[0]} — Workspace", "owner_id": str(user.id)}).execute().data
        if workspace:
            workspace_id = workspace[0]["id"]
            db.table("workspace_members").insert({"workspace_id": workspace_id, "user_id": str(user.id), "role": "owner"}).execute()
            db.table("subscriptions").insert({"workspace_id": workspace_id, "plan": "free", "max_groups": 1, "max_users": 100}).execute()
        session = getattr(result, "session", None)
        return {"ok": True, "needs_email_confirmation": session is None, "access_token": getattr(session, "access_token", None), "message": "Conta criada. Verifique seu e-mail se a confirmação estiver ativada."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "Não foi possível criar a conta.")

@router.post("/api/auth/reset")
async def auth_reset(payload: dict):
    email = str(payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Informe seu e-mail.")
    try:
        get_supabase().auth.reset_password_for_email(email, {"redirect_to": settings.dashboard_url.rstrip("/") + "/?reset=1"})
    except Exception:
        pass
    return {"ok": True, "message": "Se o e-mail estiver cadastrado, enviaremos as instruções de redefinição."}

@router.post("/api/auth/password")
async def auth_password(payload: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    password = str(payload.get("password") or "")
    if len(password) < 8:
        raise HTTPException(400, "A nova senha precisa ter pelo menos 8 caracteres.")
    try:
        get_supabase().auth.update_user({"password": password})
        return {"ok": True}
    except Exception:
        raise HTTPException(400, "Não foi possível alterar a senha.")


class RulesUpdate(BaseModel):
    require_photo: bool = True
    require_first_name: bool = True
    require_last_name: bool = True
    require_username: bool = True
    ignore_admins: bool = True
    action: str = Field(pattern="^(review|restrict|ban)$")
    dry_run: bool = True

@router.get("/api/admin/summary")
async def admin_summary(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    profiles = db.table("profiles").select("id,role,status,created_at").execute().data or []
    workspaces = db.table("workspaces").select("id,name,owner_id,created_at").execute().data or []
    chats = db.table("telegram_chats").select("id,workspace_id,is_active").execute().data or []
    return {"ok": True, "stats": {"customers": sum(p.get("role")=="customer" for p in profiles), "admins": sum(p.get("role")=="admin" for p in profiles), "active": sum(p.get("status")=="active" for p in profiles), "suspended": sum(p.get("status")=="suspended" for p in profiles), "workspaces": len(workspaces), "groups": sum(c.get("is_active") for c in chats)}}

@router.get("/api/admin/customers")
async def admin_customers(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    rows = db.table("profiles").select("id,full_name,role,status,created_at").order("created_at", desc=True).execute().data or []
    workspaces = db.table("workspaces").select("id,name,owner_id").execute().data or []
    subscriptions = db.table("subscriptions").select("workspace_id,plan,status,max_groups,max_users,current_period_end").execute().data or []
    chats = db.table("telegram_chats").select("id,title,workspace_id,is_active").execute().data or []
    ws_by_owner = {}
    for ws in workspaces:
        ws_by_owner.setdefault(ws.get("owner_id"), []).append(ws)
    sub_by_ws = {x["workspace_id"]: x for x in subscriptions}
    groups_by_ws = {}
    for chat in chats:
        if chat.get("is_active"):
            groups_by_ws[chat.get("workspace_id")] = groups_by_ws.get(chat.get("workspace_id"), 0) + 1
    customers = []
    for row in rows:
        item = {**row}
        item["workspaces"] = [{
            "id": ws["id"],
            "name": ws["name"],
            "groups": groups_by_ws.get(ws["id"], 0),
            "subscription": sub_by_ws.get(ws["id"]),
        } for ws in ws_by_owner.get(row["id"], [])]
        customers.append(item)
    return {"ok": True, "customers": customers}

@router.get("/api/admin/workspaces")
async def admin_workspaces(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    rows = db.table("workspaces").select("id,name,owner_id,created_at").order("created_at", desc=True).execute().data or []
    subscriptions = db.table("subscriptions").select("workspace_id,plan,status,max_groups,max_users,current_period_end").execute().data or []
    chats = db.table("telegram_chats").select("id,title,telegram_chat_id,workspace_id,is_active").execute().data or []
    sub_by_ws = {x["workspace_id"]: x for x in subscriptions}
    result = []
    for ws in rows:
        result.append({**ws, "subscription": sub_by_ws.get(ws["id"]), "groups": [c for c in chats if c.get("workspace_id") == ws["id"]]})
    return {"ok": True, "workspaces": result}


class SubscriptionUpdate(BaseModel):
    plan: str = Field(pattern="^(free|pro|business)$")
    status: str = Field(pattern="^(active|past_due|canceled|suspended)$")
    max_groups: int = Field(ge=0, le=10000)
    max_users: int = Field(ge=1, le=100000)

class CustomerStatusUpdate(BaseModel):
    status: str = Field(pattern="^(active|suspended|inactive)$")

class ChatAssignmentUpdate(BaseModel):
    workspace_id: str

PLAN_DEFAULTS = {
    "free": {"max_groups": 1, "max_users": 100},
    "pro": {"max_groups": 5, "max_users": 1000},
    "business": {"max_groups": 50, "max_users": 10000},
}

@router.post("/api/admin/customers/{user_id}/status")
async def admin_customer_status(user_id: str, payload: CustomerStatusUpdate, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    rows = db.table("profiles").select("id,role").eq("id", user_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, "Cliente não encontrado.")
    if rows[0].get("role") == "admin" and payload.status != "active":
        raise HTTPException(400, "Não é permitido suspender ou inativar um administrador por este painel.")
    db.table("profiles").update({"status": payload.status}).eq("id", user_id).execute()
    workspaces = db.table("workspaces").select("id").eq("owner_id", user_id).execute().data or []
    for ws in workspaces:
        sub_status = "suspended" if payload.status == "suspended" else ("canceled" if payload.status == "inactive" else "active")
        db.table("subscriptions").update({"status": sub_status}).eq("workspace_id", ws["id"]).execute()
    return {"ok": True, "status": payload.status}

@router.put("/api/admin/workspaces/{workspace_id}/subscription")
async def admin_workspace_subscription(workspace_id: str, payload: SubscriptionUpdate, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    ws = db.table("workspaces").select("id").eq("id", workspace_id).limit(1).execute().data or []
    if not ws:
        raise HTTPException(404, "Organização não encontrada.")
    db.table("subscriptions").upsert({
        "workspace_id": workspace_id,
        "plan": payload.plan,
        "status": payload.status,
        "max_groups": payload.max_groups,
        "max_users": payload.max_users,
    }, on_conflict="workspace_id").execute()
    return {"ok": True}

@router.post("/api/admin/workspaces/{workspace_id}/plan-default")
async def admin_workspace_plan_default(workspace_id: str, payload: dict, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    plan = str(payload.get("plan") or "").lower()
    if plan not in PLAN_DEFAULTS:
        raise HTTPException(400, "Plano inválido.")
    db = get_supabase()
    db.table("subscriptions").upsert({
        "workspace_id": workspace_id,
        "plan": plan,
        "status": "active",
        **PLAN_DEFAULTS[plan],
    }, on_conflict="workspace_id").execute()
    return {"ok": True, "plan": plan, **PLAN_DEFAULTS[plan]}

@router.put("/api/admin/chats/{chat_id}/workspace")
async def admin_chat_workspace(chat_id: str, payload: ChatAssignmentUpdate, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    chat = db.table("telegram_chats").select("id").eq("id", chat_id).limit(1).execute().data or []
    workspace = db.table("workspaces").select("id,name").eq("id", payload.workspace_id).limit(1).execute().data or []
    if not chat:
        raise HTTPException(404, "Grupo não encontrado.")
    if not workspace:
        raise HTTPException(404, "Organização de destino não encontrada.")
    db.table("telegram_chats").update({"workspace_id": payload.workspace_id}).eq("id", chat_id).execute()
    return {"ok": True, "workspace": workspace[0]}

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"})

@router.get("/api/dashboard/chats")
async def chats(authorization: str | None = Header(default=None)):
    user = check_token(authorization)
    db = get_supabase()
    memberships = db.table("workspace_members").select("workspace_id").eq("user_id", str(user.id)).execute().data or []
    workspace_ids = [m.get("workspace_id") for m in memberships if m.get("workspace_id")]
    if not workspace_ids:
        return {"ok": True, "chats": []}
    rows = db.table("telegram_chats").select("id,telegram_chat_id,title,chat_type,is_active").eq("is_active", True).in_("workspace_id", workspace_ids).order("title").execute().data or []
    result = []
    for chat in rows:
        rules = db.table("moderation_rules").select("require_photo,require_first_name,require_last_name,require_username,ignore_admins,action,dry_run").eq("chat_id", chat["id"]).limit(1).execute()
        scans = db.table("member_scans").select("id", count="exact").eq("chat_id", chat["id"]).execute()
        events = db.table("moderation_events").select("id", count="exact").eq("chat_id", chat["id"]).execute()
        result.append({**chat, "rules": rules.data[0] if rules.data else None, "analyses": scans.count or 0, "events": events.count or 0})
    return {"ok": True, "chats": result}

@router.get("/api/dashboard/chats/{chat_id}/rules")
async def get_rules(chat_id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    rows = get_supabase().table("moderation_rules").select("chat_id,require_photo,require_first_name,require_last_name,require_username,ignore_admins,action,dry_run").eq("chat_id", chat_id).limit(1).execute().data
    if not rows:
        raise HTTPException(404, "Regras não encontradas.")
    return {"ok": True, "rules": rows[0]}

@router.put("/api/dashboard/chats/{chat_id}/rules")
async def update_rules(chat_id: str, payload: RulesUpdate, authorization: str | None = Header(default=None)):
    check_token(authorization)
    action = "review" if payload.dry_run else payload.action
    db = get_supabase()
    db.table("moderation_rules").update({"require_photo": payload.require_photo, "require_first_name": payload.require_first_name, "require_last_name": payload.require_last_name, "require_username": payload.require_username, "ignore_admins": payload.ignore_admins, "action": action, "dry_run": payload.dry_run}).eq("chat_id", chat_id).execute()
    return await get_rules(chat_id, authorization)

@router.get("/api/dashboard/chats/{chat_id}/overview")
async def overview(chat_id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    db = get_supabase()
    scans = db.table("member_scans").select("violations,action_taken,scanned_at").eq("chat_id", chat_id).execute().data or []
    events = db.table("moderation_events").select("id,telegram_user_id,event_type,details,created_at").eq("chat_id", chat_id).order("created_at", desc=True).limit(10).execute().data or []
    rules = db.table("moderation_rules").select("dry_run").eq("chat_id", chat_id).limit(1).execute().data
    daily = {}
    for row in scans:
        stamp = row.get("scanned_at")
        if not stamp:
            continue
        day = str(stamp)[:10]
        daily.setdefault(day, 0)
        daily[day] += 1
    return {"ok": True, "stats": {"scans": len(scans), "violations": sum(bool(x.get("violations")) for x in scans), "restricts": sum(x.get("action_taken") == "restrict" for x in scans), "bans": sum(x.get("action_taken") == "ban" for x in scans), "dry_run": bool(rules[0]["dry_run"]) if rules else True, "last_event": events[0]["created_at"] if events else None}, "recent_events": events, "daily": [{"date": k, "scans": daily[k]} for k in sorted(daily)[-14:]]}

@router.get("/api/dashboard/chats/{chat_id}/members")
async def members(chat_id: str, authorization: str | None = Header(default=None), q: str = Query(default=""), action: str = Query(default="all")):
    check_token(authorization)
    rows = get_supabase().table("member_scans").select("telegram_user_id,username,first_name,last_name,violations,action_taken,scanned_at").eq("chat_id", chat_id).order("scanned_at", desc=True).limit(100).execute().data or []
    q = q.strip().lower()
    if q:
        rows = [r for r in rows if q in str(r.get("telegram_user_id", "")).lower() or q in str(r.get("username") or "").lower() or q in str(r.get("first_name") or "").lower() or q in str(r.get("last_name") or "").lower()]
    if action != "all":
        rows = [r for r in rows if r.get("action_taken") == action]
    return {"ok": True, "members": rows}

@router.post("/api/dashboard/chats/{chat_id}/members/{telegram_user_id}/ban")
async def ban_member(chat_id: str, telegram_user_id: int, authorization: str | None = Header(default=None)):
    check_token(authorization)
    db = get_supabase()
    chat_rows = db.table("telegram_chats").select("telegram_chat_id").eq("id", chat_id).limit(1).execute().data or []
    if not chat_rows:
        raise HTTPException(404, "Grupo não encontrado.")
    telegram_chat_id = chat_rows[0]["telegram_chat_id"]
    if not settings.telegram_bot_token.strip():
        raise HTTPException(503, "Token do Telegram não configurado.")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/banChatMember"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json={"chat_id": telegram_chat_id, "user_id": telegram_user_id})
    data = response.json()
    if not response.is_success or not data.get("ok"):
        raise HTTPException(400, data.get("description", "Telegram recusou o banimento."))
    latest = db.table("member_scans").select("id").eq("chat_id", chat_id).eq("telegram_user_id", telegram_user_id).order("scanned_at", desc=True).limit(1).execute().data or []
    if latest:
        db.table("member_scans").update({"action_taken": "ban"}).eq("id", latest[0]["id"]).execute()
    db.table("moderation_events").insert({"chat_id": chat_id, "telegram_user_id": telegram_user_id, "event_type": "manual_ban", "details": {"source": "dashboard"}}).execute()
    return {"ok": True, "message": "Usuário banido com sucesso."}

@router.get("/api/dashboard/chats/{chat_id}/events")
async def events(chat_id: str, authorization: str | None = Header(default=None), event_type: str = Query(default="all")):
    check_token(authorization)
    rows = get_supabase().table("moderation_events").select("id,telegram_user_id,event_type,details,created_at").eq("chat_id", chat_id).order("created_at", desc=True).limit(100).execute().data or []
    if event_type != "all":
        rows = [r for r in rows if r.get("event_type") == event_type]
    return {"ok": True, "events": rows}

HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SentinelChat — Painel</title>
<style>
body{margin:0;background:#08101d;color:#e8eef7;font-family:Arial,sans-serif}.wrap{max-width:1280px;margin:auto;padding:24px}.card{background:#101a2a;border:1px solid #26364d;border-radius:16px;padding:18px;margin-bottom:16px}header{display:flex;justify-content:space-between;align-items:center}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#0d1726;border:1px solid #293b54;border-radius:14px;padding:17px}.value{font-size:30px;font-weight:800}.layout{display:grid;grid-template-columns:220px 1fr;gap:16px}.nav button{display:block;width:100%;padding:12px;margin:4px 0;background:transparent;color:#fff;border:0;text-align:left;border-radius:9px}.nav button.active{background:#1b2a40;border-left:3px solid #ff8a00}.section{display:block}.section.active{display:block}.rules{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.row{display:flex;justify-content:space-between;padding:12px;background:#0d1726;border:1px solid #293b54;border-radius:10px}input,select,button{padding:10px;border-radius:9px;border:1px solid #344863;background:#0b1524;color:#fff}button{background:#ff8a00;color:#111;font-weight:700;cursor:pointer}.toolbar{display:flex;gap:8px;flex-wrap:wrap}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:9px;border-bottom:1px solid #243349;text-align:left}.muted{color:#91a2b8;font-size:13px}.danger{color:#ff8d98}.ok{color:#72e0a1}.chart{height:160px;display:grid;grid-template-columns:repeat(14,1fr);gap:6px;align-items:end}.day{height:145px;display:flex;flex-direction:column;justify-content:end;align-items:center}.bar{width:70%;background:#ff8a00;border-radius:5px 5px 0 0;min-height:3px}@media(max-width:850px){.layout{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.wrap{padding:12px}.grid,.rules{grid-template-columns:1fr}}
</style><script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script><script>
window.enter = async function(){
  const email = document.getElementById('email')?.value.trim() || '';
  const password = document.getElementById('password')?.value || '';
  const msg = document.getElementById('msg');
  if (!email || !password) { if(msg) msg.textContent='Informe e-mail e senha.'; return; }
  if(msg) msg.textContent='Entrando...';
  try {
    const r = await fetch('/api/auth/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email,password})
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.detail || 'Falha no login.');
    if(!d.access_token) throw new Error('Login não retornou um token de acesso.');
    localStorage.setItem('sc_access_token',d.access_token);
    localStorage.setItem('sc_user',JSON.stringify(d.user||{}));
    if(msg) msg.innerHTML='<p class="ok">✓ Login realizado. Carregando painel...</p>';
    location.reload();
  } catch(e) {
    if(msg) msg.innerHTML='<p class="danger">'+String(e.message||e)+'</p>';
  }
};
</script></head><body><div class="wrap">
<header><div><h1>🛡️ SentinelChat</h1><div class="muted">Proteção profissional da comunidade</div></div><div class="toolbar"><button onclick="refresh()">↻ Atualizar</button><button onclick="logout()">Sair</button></div></header>
<div id="app" style="display:none"><div class="card"><div class="muted">Grupo protegido</div><select id="chat" onchange="changeChat()" style="width:100%"></select><div id="info" class="muted"></div></div>
<div class="layout"><nav class="card nav"><button class="active" onclick="tab('overview',this)">📊 Visão geral</button><button onclick="tab('members',this)">👥 Membros</button><button onclick="tab('events',this)">📋 Eventos</button><button onclick="tab('settings',this)">⚙️ Configurações</button><button id="adminNav" style="display:none" onclick="tab('admin',this)">👑 Administração</button></nav>
<main>
<section id="admin" class="section"><div class="card"><h2>Administração do SaaS</h2><p class="muted">Clientes, planos, limites e grupos em um único lugar.</p><div class="grid"><div class="stat">Clientes<div id="aCustomers" class="value">—</div></div><div class="stat">Ativos<div id="aActive" class="value">—</div></div><div class="stat">Suspensos<div id="aSuspended" class="value">—</div></div><div class="stat">Grupos<div id="aGroups" class="value">—</div></div></div></div><div class="card"><h2>Clientes</h2><div id="adminCustomers"></div></div><div class="card"><h2>Organizações e grupos</h2><div id="adminWorkspaces"></div></div></section><section id="overview" class="section active"><div class="grid"><div class="stat">Analisados<div id="s1" class="value">—</div></div><div class="stat">Violações<div id="s2" class="value">—</div></div><div class="stat">Restrições<div id="s3" class="value">—</div></div><div class="stat">Banimentos<div id="s4" class="value">—</div></div></div><div class="card"><h2>Resumo</h2><div id="summary"></div></div><div class="card"><h2>Atividade recente</h2><div id="recentActivity"></div></div><div class="card"><h2>Analytics — últimos 14 dias</h2><div id="chart" class="chart"></div></div></section>
<section id="moderation" class="section"><div class="card"><h2>Modo de proteção</h2><div class="rules"><div><div class="muted">Ação</div><select id="action" style="width:100%"><option value="review">Revisão</option><option value="restrict">Restringir</option><option value="ban">Banir</option></select></div><label class="row">DRY-RUN <input id="dry" type="checkbox"></label></div></div><div class="card"><h2>Regras de entrada</h2><div class="rules"><label class="row">Foto <input id="photo" type="checkbox"></label><label class="row">Primeiro nome <input id="first" type="checkbox"></label><label class="row">Sobrenome <input id="last" type="checkbox"></label><label class="row">Username <input id="user" type="checkbox"></label><label class="row">Ignorar admins <input id="admins" type="checkbox"></label></div><br><button onclick="saveRules()">Salvar</button><div id="save"></div></div></section>
<section id="members" class="section"><div class="card"><h2>Central de membros</h2><div class="toolbar"><input id="mq" placeholder="Buscar nome, username ou ID" oninput="loadMembers()" style="flex:1"><select id="ma" onchange="loadMembers()"><option value="all">Todas</option><option value="restrict">Restringidos</option><option value="ban">Banidos</option><option value="review">Revisão</option></select></div><div id="membersContent"></div></div></section>
<section id="events" class="section"><div class="card"><h2>Central de eventos</h2><select id="et" onchange="loadEvents()"><option value="all">Todos</option><option value="member_violation">Violações</option><option value="moderation_error">Erros</option></select><div id="eventsContent"></div></div></section>
<section id="settings" class="section">
<div class="card"><h2>Configurações</h2><div id="settingsContent"></div></div><div class="card"><h2>Minha conta</h2><div id="accountInfo" class="muted"></div><h3>Alterar senha</h3><input id="newPassword" type="password" placeholder="Nova senha (mínimo 8 caracteres)" style="width:100%"><br><br><button onclick="changePassword()">Alterar senha</button><div id="passwordStatus" class="muted"></div></div>
<div class="card"><h2>Regras de entrada</h2><div class="settings-grid">
<label>Foto de perfil <input id="cfg_photo" type="checkbox"></label>
<label>Primeiro nome <input id="cfg_first" type="checkbox"></label>
<label>Sobrenome <input id="cfg_last" type="checkbox"></label>
<label>Username <input id="cfg_user" type="checkbox"></label>
<label>Ignorar administradores <input id="cfg_admin" type="checkbox"></label>
<label>DRY-RUN <input id="cfg_dry" type="checkbox"></label></div>
<label>Modo de moderação <select id="cfg_action"><option value="review">Revisão</option><option value="restrict">Restrição</option><option value="ban">Banimento</option></select></label>
<button onclick="saveConfig()">Salvar configurações</button><div id="cfg_status" class="muted"></div></div>
</section>
</main></div></div></div>
<script>
let token=localStorage.getItem('sc_access_token')||'',cid='',currentUser=null,supaClient=null;
document.addEventListener("submit",e=>{e.preventDefault();e.stopPropagation();return false},true);
document.addEventListener("keydown",e=>{if(e.key==="Enter" && (e.target?.id==="email" || e.target?.id==="password")){e.preventDefault();e.stopPropagation();enter(e)}},true);
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const hdr=()=>({'Authorization':'Bearer '+token,'Content-Type':'application/json'});
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{...hdr(),...(opt.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.detail||'Erro no painel.');return d}
async function enter(event){if(event){event.preventDefault();event.stopPropagation()}const email=$('email').value.trim(),password=$('password').value;if(!email||!password){$('msg').textContent='Informe e-mail e senha.';return}$('msg').textContent='Entrando...';try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||'Falha no login');if(!d.access_token)throw Error('Login não retornou um token de acesso.');token=d.access_token;currentUser=d.user||null;localStorage.setItem('sc_access_token',token);localStorage.setItem('sc_user',JSON.stringify(currentUser||{}));$('login').style.display='none';$('app').style.display='block';$('msg').innerHTML='<p class="ok">✓ Login realizado. Carregando painel...</p>';await loadChats()}catch(e){$('login').style.display='block';$('app').style.display='none';$('msg').innerHTML='<p class="danger">'+esc(e.message)+'</p>';console.error('LOGIN_ERROR',e)}}
function logout(){localStorage.removeItem('sc_access_token');localStorage.removeItem('sc_user');location.reload()}
function tab(id,b){document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));$(id).classList.add('active');document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');if(id==='admin')loadAdmin()}
function showSignup(event){if(event){event.preventDefault();event.stopPropagation()}const name=prompt('Nome completo:');if(name===null)return;const email=prompt('E-mail:');if(!email)return;const password=prompt('Senha (mínimo 8 caracteres):');if(password===null)return;if(password.length<8){$('msg').textContent='A senha precisa ter pelo menos 8 caracteres.';return}$('msg').textContent='Criando conta...';fetch('/api/auth/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({full_name:name,email,password})}).then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.detail||'Não foi possível criar a conta.');if(d.access_token){token=d.access_token;currentUser=d.user;localStorage.setItem('sc_access_token',token);localStorage.setItem('sc_user',JSON.stringify(currentUser));$('msg').textContent='Conta criada. Entrando...';await loadChats();return}$('msg').innerHTML='<p class="ok">✓ Conta criada. '+esc(d.message||'Confirme seu e-mail para entrar.')+'</p>'}).catch(e=>{$('msg').innerHTML='<p class="danger">'+esc(e.message)+'</p>'})}
async function resetPassword(event){if(event){event.preventDefault();event.stopPropagation()}const email=$('email').value.trim()||prompt('Informe seu e-mail:');if(!email)return;try{const d=await fetch('/api/auth/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})}).then(r=>r.json());$('msg').textContent=d.message||'Verifique seu e-mail.'}catch(e){$('msg').textContent='Solicitação enviada.'}}
async function changePassword(){const password=$('newPassword').value;if(password.length<8){$('passwordStatus').textContent='Use pelo menos 8 caracteres.';return}try{await api('/api/auth/password',{method:'POST',body:JSON.stringify({password})});$('passwordStatus').textContent='Senha alterada com sucesso.';$('newPassword').value=''}catch(e){$('passwordStatus').textContent=e.message}}
document.addEventListener("DOMContentLoaded",()=>{\n  $("loginBtn")?.addEventListener("click",e=>{e.preventDefault();e.stopImmediatePropagation();enter(e)});\n  $("signupBtn")?.addEventListener("click",e=>{e.preventDefault();e.stopImmediatePropagation();showSignup(e)});\n  $("resetBtn")?.addEventListener("click",e=>{e.preventDefault();e.stopImmediatePropagation();resetPassword(e)});\n});\nasync function bootstrapAuth(){try{const cfg=await fetch('/api/auth/public-config').then(r=>r.json());if(cfg.url&&cfg.publishable_key&&window.supabase){supaClient=window.supabase.createClient(cfg.url,cfg.publishable_key);const {data}=await supaClient.auth.getSession();if(data.session&&!token){token=data.session.access_token;localStorage.setItem('sc_access_token',token);currentUser=JSON.parse(localStorage.getItem('sc_user')||'null')}}}catch(e){}if(token)loadChats()}
async function loadAdmin(){try{const [summary,customers,workspaces]=await Promise.all([api('/api/admin/summary'),api('/api/admin/customers'),api('/api/admin/workspaces')]);$('aCustomers').textContent=summary.stats.customers;$('aActive').textContent=summary.stats.active;$('aSuspended').textContent=summary.stats.suspended;$('aGroups').textContent=summary.stats.groups;$('adminCustomers').innerHTML=customers.customers.map(x=>{const ws=(x.workspaces||[])[0],sub=ws?.subscription||{};return '<div class="card"><div class="row"><span><b>'+esc(x.full_name||'Sem nome')+'</b><br><span class="muted">'+esc(x.id)+'</span></span><span>'+esc(x.status)+'</span></div><div class="toolbar" style="margin-top:10px"><button onclick="setCustomerStatus(\''+x.id+'\',\''+(x.status==='active'?'suspended':'active')+'\')">'+(x.status==='active'?'Suspender':'Ativar')+'</button>'+(ws?'<select id="plan_'+ws.id+'"><option value="free" '+(sub.plan==='free'?'selected':'')+'>Free</option><option value="pro" '+(sub.plan==='pro'?'selected':'')+'>Pro</option><option value="business" '+(sub.plan==='business'?'selected':'')+'>Business</option></select><input id="groups_'+ws.id+'" type="number" min="0" value="'+esc(sub.max_groups??1)+'" title="Limite de grupos" style="width:90px"><input id="users_'+ws.id+'" type="number" min="1" value="'+esc(sub.max_users??100)+'" title="Limite de usuários" style="width:110px"><button onclick="saveSubscription(\''+ws.id+'\')">Salvar plano/limites</button>':'<span class="muted">Sem organização vinculada</span>')+'</div></div>'}).join('')||'<p class="muted">Nenhum cliente.</p>';$('adminWorkspaces').innerHTML='<table class="table"><tr><th>Grupo</th><th>Organização</th><th>Plano</th><th>Status</th><th>Ação</th></tr>'+workspaces.workspaces.flatMap(w=>(w.groups||[]).map(c=>'<tr><td>'+esc(c.title||c.telegram_chat_id)+'</td><td><select id="ws_'+c.id+'">'+workspaces.workspaces.map(dest=>'<option value="'+dest.id+'" '+(dest.id===c.workspace_id?'selected':'')+'>'+esc(dest.name)+'</option>').join('')+'</select></td><td>'+esc(w.subscription?.plan||'free')+'</td><td>'+esc(w.subscription?.status||'—')+'</td><td><button onclick="assignChat(\''+c.id+'\')">Vincular</button></td></tr>')).join('')+'</table>'}catch(e){$('adminCustomers').innerHTML='<p class="danger">'+esc(e.message)+'</p>'}}
async function setCustomerStatus(id,status){if(!confirm(status==='suspended'?'Suspender este cliente?':'Reativar este cliente?'))return;try{await api('/api/admin/customers/'+id+'/status',{method:'POST',body:JSON.stringify({status})});await loadAdmin()}catch(e){alert(e.message)}}
async function saveSubscription(workspaceId){const plan=$('plan_'+workspaceId).value;const defaults={free:[1,100],pro:[5,1000],business:[50,10000]};const [dg,du]=defaults[plan];const groups=Math.max(0,Number($('groups_'+workspaceId).value||dg));const users=Math.max(1,Number($('users_'+workspaceId).value||du));try{await api('/api/admin/workspaces/'+workspaceId+'/subscription',{method:'PUT',body:JSON.stringify({plan,status:'active',max_groups:groups,max_users:users})});alert('Plano e limites salvos.');await loadAdmin()}catch(e){alert(e.message)}}
async function assignChat(chatId){const workspaceId=$('ws_'+chatId).value;try{await api('/api/admin/chats/'+chatId+'/workspace',{method:'PUT',body:JSON.stringify({workspace_id:workspaceId})});alert('Grupo vinculado com sucesso.');await loadAdmin();await loadChats()}catch(e){alert(e.message)}}
async function loadChats(){try{const d=await api('/api/dashboard/chats');$('login').style.display='none';$('app').style.display='block';currentUser=currentUser||JSON.parse(localStorage.getItem('sc_user')||'null');if(currentUser){$('accountInfo').textContent=(currentUser.email||'')+' · '+(currentUser.role||'customer');if(currentUser.role==='admin')$('adminNav').style.display='block'}const s=$('chat');if(!s)throw Error('Interface do painel incompleta.');s.innerHTML='';const chats=Array.isArray(d.chats)?d.chats:[];chats.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.title+' · '+c.telegram_chat_id;s.appendChild(o)});if(!chats.length){cid='';$('info').textContent='Nenhum grupo vinculado a esta conta.';return}cid=cid&&chats.some(c=>c.id===cid)?cid:chats[0].id;s.value=cid;await refresh()}catch(e){$('login').style.display='block';$('app').style.display='none';$('msg').innerHTML='<p class="danger">Não foi possível carregar o painel: '+esc(e.message)+'</p>';console.error('LOAD_CHATS_ERROR',e)}}
async function refresh(){if(!cid)return;const b=document.querySelector('.toolbar button');if(b){b.disabled=true;b.textContent='↻ Atualizando...'}try{await Promise.all([loadRules(),loadOverview(),loadMembers(),loadEvents()]);const d=await api('/api/dashboard/chats'),c=d.chats.find(x=>x.id===cid);$('info').textContent=c?c.title+' · '+c.analyses+' análises · '+c.events+' eventos · '+(c.rules?.dry_run?'DRY-RUN':'Modo '+(c.rules?.action||'review')):''}catch(e){$('info').textContent='Erro ao atualizar: '+e.message;console.error(e)}finally{if(b){b.disabled=false;b.textContent='↻ Atualizar'}}}
function changeChat(){cid=$('chat').value;refresh()}
async function saveConfig(){const payload={require_photo:$('cfg_photo').checked,require_first_name:$('cfg_first').checked,require_last_name:$('cfg_last').checked,require_username:$('cfg_user').checked,ignore_admins:$('cfg_admin').checked,dry_run:$('cfg_dry').checked,action:$('cfg_action').value};try{await api('/api/dashboard/chats/'+cid+'/rules',{method:'PUT',body:JSON.stringify(payload)});$('cfg_status').textContent='Configurações salvas com sucesso.';await loadRules();}catch(e){$('cfg_status').textContent='Erro ao salvar: '+e.message;}}
async function loadRules(){const d=await api('/api/dashboard/chats/'+cid+'/rules'),x=d.rules;$('photo').checked=!!x.require_photo;$('first').checked=!!x.require_first_name;$('last').checked=!!x.require_last_name;$('user').checked=!!x.require_username;$('admins').checked=!!x.ignore_admins;$('action').value=x.action||'review';$('dry').checked=!!x.dry_run;$('cfg_photo').checked=!!x.require_photo;$('cfg_first').checked=!!x.require_first_name;$('cfg_last').checked=!!x.require_last_name;$('cfg_user').checked=!!x.require_username;$('cfg_admin').checked=!!x.ignore_admins;$('cfg_action').value=x.action||'review';$('cfg_dry').checked=!!x.dry_run;$('settingsContent').innerHTML='<p><b>Modo atual:</b> '+(x.dry_run?'DRY-RUN':esc(x.action))+'</p><p class="muted">As regras abaixo são aplicadas automaticamente aos novos membros.</p><p class="muted">Última atualização: '+esc(dt(x.updated_at)||'—')+'</p>'}
async function saveRules(){const b={require_photo:$('photo').checked,require_first_name:$('first').checked,require_last_name:$('last').checked,require_username:$('user').checked,ignore_admins:$('admins').checked,action:$('action').value,dry_run:$('dry').checked};$('save').textContent='Salvando...';try{await api('/api/dashboard/chats/'+cid+'/rules',{method:'PUT',body:JSON.stringify(b)});$('save').innerHTML='<span class="ok">✓ Salvo.</span>';await loadRules()}catch(e){$('save').innerHTML='<span class="danger">'+esc(e.message)+'</span>'}}
async function loadOverview(){const d=await api('/api/dashboard/chats/'+cid+'/overview');$('s1').textContent=d.stats.scans;$('s2').textContent=d.stats.violations;$('s3').textContent=d.stats.restricts;$('s4').textContent=d.stats.bans;$('summary').innerHTML='<p>'+(d.stats.dry_run?'🧪 DRY-RUN ativo':'🛡️ Moderação real ativa')+'</p><p class="muted">Último evento: '+esc(dt(d.stats.last_event)||'Nenhum')+'</p>';$('recentActivity').innerHTML=(d.recent_events||[]).length?(d.recent_events||[]).slice(0,6).map(x=>'<div class="row"><span><b>'+esc(x.event_type)+'</b><br><span class="muted">Usuário: '+esc(x.telegram_user_id||'—')+'</span></span><span class="muted">'+esc(dt(x.created_at))+'</span></div>').join(''):'<p class="muted">Nenhuma atividade recente.</p>';const a=d.daily||[],m=Math.max(1,...a.map(x=>x.scans));$('chart').innerHTML=a.length?a.map(x=>'<div class="day"><div class="bar" style="height:'+Math.max(4,Math.round(x.scans/m*125))+'px"></div><span class="small">'+x.scans+'</span><span class="small">'+x.date.slice(5)+'</span></div>').join(''):'<span class="muted">Sem dados.</span>'}
function dt(x){return x?new Date(x).toLocaleString('pt-BR'):'—'}
async function banMember(uid){if(!confirm('Banir este usuário do grupo?'))return;try{await api('/api/dashboard/chats/'+cid+'/members/'+uid+'/ban',{method:'POST'});alert('Usuário banido com sucesso.');await loadMembers();await loadOverview()}catch(e){alert('Não foi possível banir: '+e.message)}}
async function loadMembers(){const q=encodeURIComponent($('mq')?.value||''),a=encodeURIComponent($('ma')?.value||'all'),d=await api('/api/dashboard/chats/'+cid+'/members?q='+q+'&action='+a);if(!d.members.length){$('membersContent').innerHTML='<p class="muted">Nenhum resultado.</p>';return}$('membersContent').innerHTML='<table class="table"><tr><th>Usuário</th><th>Username</th><th>Violações</th><th>Ação</th><th>Data</th></tr>'+d.members.map(x=>'<tr><td>'+esc([x.first_name,x.last_name].filter(Boolean).join(' ')||'Sem nome')+'<br>'+x.telegram_user_id+'</td><td>'+esc(x.username?'@'+x.username:'—')+'</td><td>'+esc((x.violations||[]).join(', ')||'Nenhuma')+'</td><td>'+esc(x.action_taken||'OK')+'<br><button class="dangerBtn" onclick="banMember('+x.telegram_user_id+')">Banir</button></td><td>'+esc(dt(x.scanned_at))+'</td></tr>').join('')+'</table>'}
async function loadEvents(){const t=encodeURIComponent($('et')?.value||'all'),d=await api('/api/dashboard/chats/'+cid+'/events?event_type='+t);if(!d.events.length){$('eventsContent').innerHTML='<p class="muted">Nenhum evento.</p>';return}$('eventsContent').innerHTML=d.events.map(x=>'<div class="card"><b>'+esc(x.event_type)+'</b><div class="small">Usuário: '+esc(x.telegram_user_id||'—')+' · '+esc(dt(x.created_at))+'</div><div>'+esc(JSON.stringify(x.details||{}))+'</div></div>').join('')}
bootstrapAuth()
</script></body></html>"""
