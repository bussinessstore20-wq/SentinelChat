from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import settings
from .database import get_supabase


router = APIRouter()


def require_dashboard_token(authorization: str | None) -> None:
    configured = settings.dashboard_token.strip()

    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Dashboard desativado: DASHBOARD_TOKEN não configurado.",
        )

    expected = f"Bearer {configured}"

    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Token do dashboard inválido.",
        )


class RulesUpdate(BaseModel):
    require_photo: bool = True
    require_first_name: bool = True
    require_last_name: bool = True
    require_username: bool = True
    ignore_admins: bool = True
    action: str = Field(pattern="^(review|restrict|ban)$")
    dry_run: bool = True


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    if not settings.dashboard_token.strip():
        return HTMLResponse(
            """
            <html><body style="font-family:Arial;padding:40px">
            <h1>SentinelChat</h1>
            <p>Dashboard desativado. Configure <b>DASHBOARD_TOKEN</b> no ambiente.</p>
            </body></html>
            """,
            status_code=503,
        )

    return HTMLResponse(DASHBOARD_HTML)


@router.get("/api/dashboard/chats")
async def dashboard_chats(authorization: str | None = Header(default=None)):
    require_dashboard_token(authorization)

    db = get_supabase()

    chats = (
        db.table("telegram_chats")
        .select("id,telegram_chat_id,title,chat_type,is_active")
        .eq("is_active", True)
        .order("title")
        .execute()
    )

    result = []

    for chat in chats.data or []:
        rules = (
            db.table("moderation_rules")
            .select(
                "require_photo,require_first_name,require_last_name,"
                "require_username,ignore_admins,action,dry_run"
            )
            .eq("chat_id", chat["id"])
            .limit(1)
            .execute()
        )

        scans = (
            db.table("member_scans")
            .select("id", count="exact")
            .eq("chat_id", chat["id"])
            .execute()
        )

        events = (
            db.table("moderation_events")
            .select("id", count="exact")
            .eq("chat_id", chat["id"])
            .execute()
        )

        result.append(
            {
                **chat,
                "rules": rules.data[0] if rules.data else None,
                "analyses": scans.count or 0,
                "events": events.count or 0,
            }
        )

    return {"ok": True, "chats": result}


@router.get("/api/dashboard/chats/{chat_id}/rules")
async def dashboard_get_rules(
    chat_id: str,
    authorization: str | None = Header(default=None),
):
    require_dashboard_token(authorization)

    db = get_supabase()

    result = (
        db.table("moderation_rules")
        .select(
            "chat_id,require_photo,require_first_name,require_last_name,"
            "require_username,ignore_admins,action,dry_run"
        )
        .eq("chat_id", chat_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Regras não encontradas.",
        )

    return {"ok": True, "rules": result.data[0]}


@router.put("/api/dashboard/chats/{chat_id}/rules")
async def dashboard_update_rules(
    chat_id: str,
    payload: RulesUpdate,
    authorization: str | None = Header(default=None),
):
    require_dashboard_token(authorization)

    # BAN e RESTRICT são ações reais. Para evitar uma ativação
    # acidental, o modo só fica ativo quando dry_run=false.
    if payload.dry_run:
        action = "review"
    else:
        action = payload.action

    db = get_supabase()

    result = (
        db.table("moderation_rules")
        .update(
            {
                "require_photo": payload.require_photo,
                "require_first_name": payload.require_first_name,
                "require_last_name": payload.require_last_name,
                "require_username": payload.require_username,
                "ignore_admins": payload.ignore_admins,
                "action": action,
                "dry_run": payload.dry_run,
            }
        )
        .eq("chat_id", chat_id)
        .execute()
    )

    if not result.data:
        # Supabase pode retornar zero linhas quando não encontra
        # o registro; nesse caso, confirmar se ele existe.
        check = (
            db.table("moderation_rules")
            .select("chat_id")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )

        if not check.data:
            raise HTTPException(
                status_code=404,
                detail="Grupo não encontrado.",
            )

    updated = (
        db.table("moderation_rules")
        .select(
            "chat_id,require_photo,require_first_name,require_last_name,"
            "require_username,ignore_admins,action,dry_run"
        )
        .eq("chat_id", chat_id)
        .limit(1)
        .execute()
    )

    return {
        "ok": True,
        "rules": updated.data[0] if updated.data else None,
    }


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SentinelChat — Painel</title>
<style>
:root{font-family:Inter,ui-sans-serif,Arial,sans-serif;color:#e8eef7;background:#08101d}
*{box-sizing:border-box}body{margin:0;background:#08101d}
.wrap{max-width:1280px;margin:auto;padding:24px}
header{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:22px}
.brand{display:flex;gap:12px;align-items:center}.logo{width:44px;height:44px;border-radius:13px;background:#ff8a00;display:grid;place-items:center;font-size:22px}
h1{margin:0;font-size:25px}.muted{color:#91a2b8;font-size:14px}
.card{background:#101a2a;border:1px solid #26364d;border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 8px 28px rgba(0,0,0,.14)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap}.toolbar button{margin:0}
button{border:0;border-radius:10px;padding:11px 15px;font-weight:750;cursor:pointer;background:#ff8a00;color:#111}
button.secondary{background:#25364e;color:#fff}button.danger{background:#3a2027;color:#ff9aa3}
button:disabled{opacity:.6;cursor:wait}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.stat{padding:17px;border-radius:14px;border:1px solid #293b54;background:#0d1726}
.stat .value{font-size:29px;font-weight:800;margin-top:7px}.stat .label{color:#a5b4c7;font-size:13px}
.stat.orange{border-color:#70440e}.stat.green{border-color:#185a43}.stat.red{border-color:#6a2730}.stat.blue{border-color:#244d78}
.layout{display:grid;grid-template-columns:270px 1fr;gap:16px}
.nav{position:sticky;top:16px;height:max-content}.nav button{width:100%;text-align:left;background:transparent;color:#b9c6d7;margin-bottom:6px}.nav button.active{background:#1b2a40;color:#fff;border-left:3px solid #ff8a00}
.section{display:none}.section.active{display:block}
select,input[type=password]{width:100%;padding:11px;border-radius:10px;border:1px solid #344863;background:#0b1524;color:#fff}
label.row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px;border:1px solid #293b54;border-radius:11px;background:#0d1726}
input[type=checkbox]{width:19px;height:19px}
.rulegrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
.tablewrap{overflow:auto}.table{width:100%;border-collapse:collapse;font-size:14px}.table th,.table td{padding:11px;border-bottom:1px solid #243349;text-align:left;white-space:nowrap}.table th{color:#91a2b8;font-weight:600}
.badge{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:12px;font-weight:750;background:#1d2a3c}.badge.green{color:#72e0a1;background:#123326}.badge.red{color:#ff8d98;background:#351b21}.badge.orange{color:#ffcb75;background:#38280f}.badge.blue{color:#8ec7ff;background:#152b42}
.status{padding:11px;border-radius:10px;background:#0d1726;margin-top:10px}.ok{color:#72e0a1}.warn{color:#ffcb75}.danger{color:#ff8d98}
.list{display:grid;gap:9px}.event{padding:12px;border:1px solid #26364d;border-radius:11px;background:#0d1726}.event strong{display:block;margin-bottom:4px}
.small{font-size:12px;color:#8192a8}
@media(max-width:900px){.layout{grid-template-columns:1fr}.nav{position:static;display:flex;overflow:auto;gap:6px}.nav button{width:auto;white-space:nowrap}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.wrap{padding:14px}.grid,.rulegrid{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body>
<div class="wrap">
<header>
<div class="brand"><div class="logo">🛡️</div><div><h1>SentinelChat</h1><div class="muted">Proteção e moderação da comunidade</div></div></div>
<div class="toolbar"><button class="secondary" onclick="refreshAll()">↻ Atualizar</button><button class="danger" onclick="logout()">Sair</button></div>
</header>

<div class="card" id="loginCard">
<h2>Acesso ao painel</h2>
<p class="muted">Use o DASHBOARD_TOKEN configurado no Render.</p>
<input id="token" type="password" placeholder="Token do dashboard">
<br><br><button onclick="saveToken()">Entrar</button>
<div id="loginStatus"></div>
</div>

<div id="app" style="display:none">
<div class="card">
<div class="muted">Grupo protegido</div>
<select id="chatSelect" onchange="changeChat()" style="margin-top:8px"></select>
<div id="chatInfo" class="status"></div>
</div>

<div class="layout">
<nav class="card nav">
<button class="active" data-section="overview" onclick="showSection('overview',this)">📊 Visão geral</button>
<button data-section="moderation" onclick="showSection('moderation',this)">🛡️ Moderação</button>
<button data-section="members" onclick="showSection('members',this)">👥 Membros</button>
<button data-section="events" onclick="showSection('events',this)">📋 Eventos</button>
<button data-section="settings" onclick="showSection('settings',this)">⚙️ Configurações</button>
</nav>

<main>
<section id="overview" class="section active">
<div class="grid" id="statsGrid">
<div class="stat orange"><div class="label">Membros analisados</div><div class="value" id="statScans">—</div></div>
<div class="stat red"><div class="label">Violações</div><div class="value" id="statViolations">—</div></div>
<div class="stat blue"><div class="label">Restrições</div><div class="value" id="statRestricts">—</div></div>
<div class="stat red"><div class="label">Banimentos</div><div class="value" id="statBans">—</div></div>
</div>
<div class="card" style="margin-top:16px">
<h2>Resumo de proteção</h2>
<div id="summary" class="list"></div>
</div>
</section>

<section id="moderation" class="section">
<div class="card"><h2>Modo de proteção</h2>
<div class="rulegrid">
<div><div class="muted">Ação aplicada</div><select id="action" style="margin-top:7px"><option value="review">Revisão / apenas detectar</option><option value="restrict">Restringir</option><option value="ban">Banir</option></select></div>
<label class="row">DRY-RUN ativo <input id="dryRun" type="checkbox"></label>
</div>
<p class="muted">Com DRY-RUN ativo, nenhuma punição é aplicada. Restrição e banimento só entram em vigor com o DRY-RUN desligado.</p>
</div>
<div class="card"><h2>Regras de entrada</h2>
<div class="rulegrid">
<label class="row">Foto de perfil <input id="photo" type="checkbox"></label>
<label class="row">Primeiro nome <input id="firstName" type="checkbox"></label>
<label class="row">Sobrenome <input id="lastName" type="checkbox"></label>
<label class="row">Username <input id="username" type="checkbox"></label>
<label class="row">Ignorar administradores <input id="admins" type="checkbox"></label>
</div>
<br><button onclick="saveRules()">Salvar configuração</button><div id="saveStatus" class="status"></div>
</div>
</section>

<section id="members" class="section">
<div class="card"><h2>Últimas análises</h2><div id="membersTable" class="tablewrap"></div></div>
</section>

<section id="events" class="section">
<div class="card"><h2>Eventos recentes</h2><div id="eventsList" class="list"></div></div>
</section>

<section id="settings" class="section">
<div class="card"><h2>Configurações do grupo</h2>
<p class="muted">As configurações abaixo são aplicadas diretamente às regras de moderação do grupo selecionado.</p>
<div id="settingsSummary" class="list"></div>
</div>
<div class="card"><h2>Segurança</h2><p class="muted">O painel usa um token privado armazenado no ambiente do servidor. Ele não é gravado no código-fonte.</p><button class="danger" onclick="logout()">Encerrar sessão</button></div>
</section>
</main>
</div>
</div>
</div>

<script>
let authToken=localStorage.getItem("sentinel_dashboard_token")||"";
let chats=[];let currentChatId="";
const $=id=>document.getElementById(id);
function headers(){return {"Authorization":"Bearer "+authToken,"Content-Type":"application/json"}}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]))}
function saveToken(){authToken=$("token").value.trim();if(!authToken){$("loginStatus").innerHTML='<p class="danger">Informe o token.</p>';return}localStorage.setItem("sentinel_dashboard_token",authToken);loadChats()}
function logout(){localStorage.removeItem("sentinel_dashboard_token");authToken="";location.reload()}
async function api(url,options={}){const r=await fetch(url,{...options,headers:{...headers(),...(options.headers||{})}});let d={};try{d=await r.json()}catch(e){}if(r.status===401){$("loginCard").style.display="block";$("app").style.display="none";throw new Error("Token inválido.")}if(!r.ok)throw new Error(d.detail||"Erro ao consultar o painel.");return d}
async function loadChats(){
 if(!authToken){$("loginCard").style.display="block";return}
 try{
  const d=await api("/api/dashboard/chats");chats=d.chats||[];
  $("loginCard").style.display="none";$("app").style.display="block";
  const s=$("chatSelect");s.innerHTML="";chats.forEach(c=>{const o=document.createElement("option");o.value=c.id;o.textContent=c.title+" · "+c.telegram_chat_id;s.appendChild(o)});
  if(!chats.length){$("chatInfo").innerHTML='<span class="warn">Nenhum grupo registrado ainda.</span>';return}
  currentChatId=currentChatId&&chats.some(c=>c.id===currentChatId)?currentChatId:chats[0].id;s.value=currentChatId;renderChatInfo();await refreshCurrent();
 }catch(e){$("loginStatus").innerHTML='<p class="danger">'+esc(e.message)+'</p>'}
}
function renderChatInfo(){const c=chats.find(x=>x.id===currentChatId);if(!c)return;$("chatInfo").innerHTML='<span class="ok">🟢 '+esc(c.title)+'</span> · Análises: '+c.analyses+' · Eventos: '+c.events+' · '+(c.rules?.dry_run?'DRY-RUN':'Modo '+esc(c.rules?.action||'review'))}
async function changeChat(){currentChatId=$("chatSelect").value;renderChatInfo();await refreshCurrent()}
async function refreshCurrent(){if(!currentChatId)return;await Promise.all([loadRules(),loadOverview(),loadMembers(),loadEvents()]);renderChatInfo()}
async function refreshAll(){await loadChats()}
function showSection(id,btn){document.querySelectorAll(".section").forEach(x=>x.classList.remove("active"));$(id).classList.add("active");document.querySelectorAll(".nav button").forEach(x=>x.classList.remove("active"));btn.classList.add("active")}
async function loadRules(){
 const d=await api("/api/dashboard/chats/"+currentChatId+"/rules");const x=d.rules;
 $("photo").checked=!!x.require_photo;$("firstName").checked=!!x.require_first_name;$("lastName").checked=!!x.require_last_name;$("username").checked=!!x.require_username;$("admins").checked=!!x.ignore_admins;$("action").value=x.action||"review";$("dryRun").checked=!!x.dry_run;
 $("settingsSummary").innerHTML='<div class="event"><strong>Modo</strong><span class="small">'+(x.dry_run?'DRY-RUN — somente detecção':esc(x.action))+'</span></div><div class="event"><strong>Regras</strong><span class="small">'+[x.require_photo?'foto':'',x.require_first_name?'primeiro nome':'',x.require_last_name?'sobrenome':'',x.require_username?'username':''].filter(Boolean).join(', ')||'Nenhuma'+'</span></div>';
}
async function saveRules(){
 const body={require_photo:$("photo").checked,require_first_name:$("firstName").checked,require_last_name:$("lastName").checked,require_username:$("username").checked,ignore_admins:$("admins").checked,action:$("action").value,dry_run:$("dryRun").checked};
 $("saveStatus").innerHTML="Salvando…";
 try{const d=await api("/api/dashboard/chats/"+currentChatId+"/rules",{method:"PUT",body:JSON.stringify(body)});$("saveStatus").innerHTML='<span class="ok">✓ Configuração salva e confirmada no banco.</span>';const c=chats.find(x=>x.id===currentChatId);if(c)c.rules=d.rules;renderChatInfo();loadRules()}catch(e){$("saveStatus").innerHTML='<span class="danger">'+esc(e.message)+'</span>'}
}
async function loadOverview(){
 try{const d=await api("/api/dashboard/chats/"+currentChatId+"/overview");$("statScans").textContent=d.stats.scans;$("statViolations").textContent=d.stats.violations;$("statRestricts").textContent=d.stats.restricts;$("statBans").textContent=d.stats.bans;
 $("summary").innerHTML='<div class="event"><strong>Estado</strong><span class="small">'+(d.stats.dry_run?'🧪 DRY-RUN ativo':'🛡️ Moderação real ativa')+'</span></div><div class="event"><strong>Última atividade</strong><span class="small">'+esc(d.stats.last_event||"Nenhum evento registrado")+'</span></div>'}catch(e){$("summary").innerHTML='<div class="danger">'+esc(e.message)+'</div>'}
}
async function loadMembers(){
 try{const d=await api("/api/dashboard/chats/"+currentChatId+"/members");if(!d.members.length){$("membersTable").innerHTML='<p class="muted">Nenhuma análise registrada.</p>';return}
 $("membersTable").innerHTML='<table class="table"><thead><tr><th>Usuário</th><th>Username</th><th>Violações</th><th>Ação</th><th>Data</th></tr></thead><tbody>'+d.members.map(m=>'<tr><td>'+esc([m.first_name,m.last_name].filter(Boolean).join(" ")||"Sem nome")+'<br><span class="small">'+m.telegram_user_id+'</span></td><td>'+esc(m.username?("@"+m.username):"—")+'</td><td>'+esc((m.violations||[]).join(", ")||"Nenhuma")+'</td><td>'+badge(m.action_taken)+'</td><td>'+esc(formatDate(m.scanned_at))+'</td></tr>').join("")+'</tbody></table>'}catch(e){$("membersTable").innerHTML='<p class="danger">'+esc(e.message)+'</p>'}
}
function badge(v){if(!v)return'<span class="badge green">OK</span>';let cls=v==="ban"?"red":v==="restrict"?"orange":"blue";return'<span class="badge '+cls+'">'+esc(v)+'</span>'}
async function loadEvents(){
 try{const d=await api("/api/dashboard/chats/"+currentChatId+"/events");if(!d.events.length){$("eventsList").innerHTML='<div class="muted">Nenhum evento registrado.</div>';return}
 $("eventsList").innerHTML=d.events.map(e=>'<div class="event"><strong>'+esc(e.event_type||"evento")+'</strong><span class="small">Usuário: '+esc(e.telegram_user_id||"—")+' · '+esc(formatDate(e.created_at))+'</span><div style="margin-top:6px">'+esc(JSON.stringify(e.details||{}))+'</div></div>').join("")}catch(e){$("eventsList").innerHTML='<div class="danger">'+esc(e.message)+'</div>'}
}
function formatDate(v){if(!v)return"—";try{return new Date(v).toLocaleString("pt-BR")}catch(e){return v}}
if(authToken){$("token").value=authToken;loadChats()}
</script>
</body>
</html>
"""
