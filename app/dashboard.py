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
    try:
        result = get_supabase().auth.sign_up({"email": email, "password": password, "options": {"data": {"full_name": full_name}}})
        user = getattr(result, "user", None)
        if not user:
            raise ValueError("signup failed")
        db = get_supabase()
        is_admin = bool(settings.saas_admin_email.strip()) and email == settings.saas_admin_email.strip().lower()
        profile = db.table("profiles").update({"full_name": full_name, "role": "admin" if is_admin else "customer"}).eq("id", str(user.id)).execute()
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
    return {"ok": True, "customers": rows}

@router.get("/api/admin/workspaces")
async def admin_workspaces(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = get_supabase()
    rows = db.table("workspaces").select("id,name,owner_id,created_at").order("created_at", desc=True).execute().data or []
    return {"ok": True, "workspaces": rows}

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML)

@router.get("/api/dashboard/chats")
async def chats(authorization: str | None = Header(default=None)):
    check_token(authorization)
    db = get_supabase()
    rows = db.table("telegram_chats").select("id,telegram_chat_id,title,chat_type,is_active").eq("is_active", True).order("title").execute().data or []
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
body{margin:0;background:#08101d;color:#e8eef7;font-family:Arial,sans-serif}.wrap{max-width:1280px;margin:auto;padding:24px}.card{background:#101a2a;border:1px solid #26364d;border-radius:16px;padding:18px;margin-bottom:16px}header{display:flex;justify-content:space-between;align-items:center}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#0d1726;border:1px solid #293b54;border-radius:14px;padding:17px}.value{font-size:30px;font-weight:800}.layout{display:grid;grid-template-columns:220px 1fr;gap:16px}.nav button{display:block;width:100%;padding:12px;margin:4px 0;background:transparent;color:#fff;border:0;text-align:left;border-radius:9px}.nav button.active{background:#1b2a40;border-left:3px solid #ff8a00}.section{display:none}.section.active{display:block}.rules{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.row{display:flex;justify-content:space-between;padding:12px;background:#0d1726;border:1px solid #293b54;border-radius:10px}input,select,button{padding:10px;border-radius:9px;border:1px solid #344863;background:#0b1524;color:#fff}button{background:#ff8a00;color:#111;font-weight:700;cursor:pointer}.toolbar{display:flex;gap:8px;flex-wrap:wrap}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:9px;border-bottom:1px solid #243349;text-align:left}.muted{color:#91a2b8;font-size:13px}.danger{color:#ff8d98}.ok{color:#72e0a1}.chart{height:160px;display:grid;grid-template-columns:repeat(14,1fr);gap:6px;align-items:end}.day{height:145px;display:flex;flex-direction:column;justify-content:end;align-items:center}.bar{width:70%;background:#ff8a00;border-radius:5px 5px 0 0;min-height:3px}@media(max-width:850px){.layout{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.wrap{padding:12px}.grid,.rules{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<header><div><h1>🛡️ SentinelChat</h1><div class="muted">Proteção profissional da comunidade</div></div><div class="toolbar"><button onclick="refresh()">↻ Atualizar</button><button onclick="logout()">Sair</button></div></header>
<div id="login" class="card"><h2>Acesso</h2><input id="token" type="password" placeholder="DASHBOARD_TOKEN" style="width:100%"><br><br><button onclick="enter()">Entrar</button><div id="msg"></div></div>
<div id="app" style="display:none"><div class="card"><div class="muted">Grupo protegido</div><select id="chat" onchange="changeChat()" style="width:100%"></select><div id="info" class="muted"></div></div>
<div class="layout"><nav class="card nav"><button class="active" onclick="tab('overview',this)">📊 Visão geral</button><button onclick="tab('members',this)">👥 Membros</button><button onclick="tab('events',this)">📋 Eventos</button><button onclick="tab('settings',this)">⚙️ Configurações</button></nav>
<main>
<section id="overview" class="section active"><div class="grid"><div class="stat">Analisados<div id="s1" class="value">—</div></div><div class="stat">Violações<div id="s2" class="value">—</div></div><div class="stat">Restrições<div id="s3" class="value">—</div></div><div class="stat">Banimentos<div id="s4" class="value">—</div></div></div><div class="card"><h2>Resumo</h2><div id="summary"></div></div><div class="card"><h2>Atividade recente</h2><div id="recentActivity"></div></div><div class="card"><h2>Analytics — últimos 14 dias</h2><div id="chart" class="chart"></div></div></section>
<section id="moderation" class="section"><div class="card"><h2>Modo de proteção</h2><div class="rules"><div><div class="muted">Ação</div><select id="action" style="width:100%"><option value="review">Revisão</option><option value="restrict">Restringir</option><option value="ban">Banir</option></select></div><label class="row">DRY-RUN <input id="dry" type="checkbox"></label></div></div><div class="card"><h2>Regras de entrada</h2><div class="rules"><label class="row">Foto <input id="photo" type="checkbox"></label><label class="row">Primeiro nome <input id="first" type="checkbox"></label><label class="row">Sobrenome <input id="last" type="checkbox"></label><label class="row">Username <input id="user" type="checkbox"></label><label class="row">Ignorar admins <input id="admins" type="checkbox"></label></div><br><button onclick="saveRules()">Salvar</button><div id="save"></div></div></section>
<section id="members" class="section"><div class="card"><h2>Central de membros</h2><div class="toolbar"><input id="mq" placeholder="Buscar nome, username ou ID" oninput="loadMembers()" style="flex:1"><select id="ma" onchange="loadMembers()"><option value="all">Todas</option><option value="restrict">Restringidos</option><option value="ban">Banidos</option><option value="review">Revisão</option></select></div><div id="membersContent"></div></div></section>
<section id="events" class="section"><div class="card"><h2>Central de eventos</h2><select id="et" onchange="loadEvents()"><option value="all">Todos</option><option value="member_violation">Violações</option><option value="moderation_error">Erros</option></select><div id="eventsContent"></div></div></section>
<section id="settings" class="section">
<div class="card"><h2>Configurações</h2><div id="settingsContent"></div></div>
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
let token=localStorage.getItem('sc_token')||'',cid='';const $=id=>document.getElementById(id);const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));const hdr=()=>({'Authorization':'Bearer '+token,'Content-Type':'application/json'});
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{...hdr(),...(opt.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.detail||'Erro no painel.');return d}
function enter(){token=$('token').value.trim();if(!token)return;$('msg').textContent='Conectando...';localStorage.setItem('sc_token',token);loadChats()}function logout(){localStorage.removeItem('sc_token');location.reload()}function tab(id,b){document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));$(id).classList.add('active');document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active')}
async function loadChats(){try{const d=await api('/api/dashboard/chats');$('login').style.display='none';$('app').style.display='block';const s=$('chat');s.innerHTML='';(d.chats||[]).forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.title+' · '+c.telegram_chat_id;s.appendChild(o)});if(!d.chats?.length)return;cid=cid&&d.chats.some(c=>c.id===cid)?cid:d.chats[0].id;s.value=cid;await refresh()}catch(e){$('msg').innerHTML='<p class="danger">'+esc(e.message)+'</p>'}}
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
if(token){$('token').value=token;loadChats()}
</script></body></html>"""
