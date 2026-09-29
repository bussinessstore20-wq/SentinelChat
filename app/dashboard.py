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
<title>SentinelChat — Configuração</title>
<style>
:root{font-family:Inter,Arial,sans-serif;color:#e8eef7;background:#0b1220}
*{box-sizing:border-box}body{margin:0;background:#0b1220}
.wrap{max-width:1050px;margin:auto;padding:28px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:24px}
h1{margin:0;font-size:28px}h2{margin-top:0}.muted{color:#93a4ba}
.card{background:#121c2d;border:1px solid #26364d;border-radius:16px;padding:20px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
label{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px;border:1px solid #293b54;border-radius:12px;background:#0f1827}
input[type=checkbox]{width:20px;height:20px}
select,input[type=password]{width:100%;padding:12px;border-radius:10px;border:1px solid #344863;background:#0c1524;color:#fff}
button{border:0;border-radius:10px;padding:12px 16px;font-weight:700;cursor:pointer;background:#ff8a00;color:#111}
button.secondary{background:#26364d;color:#fff}
.status{padding:12px;border-radius:10px;background:#0f1827;margin-top:12px}
.ok{color:#71e0a1}.warn{color:#ffcb6b}.danger{color:#ff7b86}
@media(max-width:700px){.grid{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body>
<div class="wrap">
<header>
<div><h1>🛡️ SentinelChat</h1><div class="muted">Configuração de proteção da comunidade</div></div>
<button class="secondary" onclick="loadChats()">Atualizar</button>
</header>

<div class="card" id="loginCard">
<h2>Acesso</h2>
<p class="muted">Informe o DASHBOARD_TOKEN configurado no Render.</p>
<input id="token" type="password" placeholder="Token do dashboard">
<br><br><button onclick="saveToken()">Entrar</button>
<div id="loginStatus"></div>
</div>

<div id="app" style="display:none">
<div class="card">
<h2>Grupo</h2>
<select id="chatSelect" onchange="loadRules()"></select>
<div id="chatInfo" class="status"></div>
</div>

<div class="card">
<h2>Regras de entrada</h2>
<div class="grid">
<label>Foto de perfil <input id="photo" type="checkbox"></label>
<label>Primeiro nome <input id="firstName" type="checkbox"></label>
<label>Sobrenome <input id="lastName" type="checkbox"></label>
<label>Username <input id="username" type="checkbox"></label>
<label>Ignorar administradores <input id="admins" type="checkbox"></label>
</div>
</div>

<div class="card">
<h2>Ação</h2>
<div class="grid">
<div>
<label style="display:block">
<div class="muted">Modo de proteção</div>
<select id="action">
<option value="review">Revisão / apenas detectar</option>
<option value="restrict">Restringir</option>
<option value="ban">Banir</option>
</select>
</label>
</div>
<label>DRY-RUN ativo <input id="dryRun" type="checkbox"></label>
</div>
<p class="muted">Com DRY-RUN ativo, nenhuma punição é aplicada. Restrição e banimento só entram em vigor quando o DRY-RUN estiver desligado.</p>
<button onclick="saveRules()">Salvar configuração</button>
<div id="saveStatus" class="status"></div>
</div>
</div>
</div>

<script>
let authToken=localStorage.getItem("sentinel_dashboard_token")||"";
const $=id=>document.getElementById(id);
function headers(){return {"Authorization":"Bearer "+authToken,"Content-Type":"application/json"}}
function saveToken(){
 authToken=$("token").value.trim();
 if(!authToken){$("loginStatus").innerHTML='<p class="danger">Informe o token.</p>';return}
 localStorage.setItem("sentinel_dashboard_token",authToken);
 loadChats();
}
async function loadChats(){
 if(!authToken){$("loginCard").style.display="block";return}
 try{
  const r=await fetch("/api/dashboard/chats",{headers:headers()});
  if(r.status===401){$("loginCard").style.display="block";$("loginStatus").innerHTML='<p class="danger">Token inválido.</p>';return}
  const d=await r.json(); if(!r.ok) throw new Error(d.detail||"Erro");
  $("loginCard").style.display="none";$("app").style.display="block";
  const s=$("chatSelect");s.innerHTML="";
  (d.chats||[]).forEach(c=>{const o=document.createElement("option");o.value=c.id;o.textContent=c.title+" ("+c.telegram_chat_id+")";s.appendChild(o)});
  if(!d.chats.length){$("chatInfo").innerHTML='<span class="warn">Nenhum grupo registrado ainda.</span>';return}
  const c=d.chats[0]; $("chatInfo").innerHTML='<span class="ok">🟢 '+c.title+'</span> · Análises: '+c.analyses+' · Eventos: '+c.events;
  await loadRules();
 }catch(e){$("loginStatus").innerHTML='<p class="danger">'+e.message+'</p>'}
}
async function loadRules(){
 const id=$("chatSelect").value;if(!id)return;
 const r=await fetch("/api/dashboard/chats/"+id+"/rules",{headers:headers()});
 const d=await r.json();if(!r.ok){$("saveStatus").innerHTML='<span class="danger">'+(d.detail||"Erro")+'</span>';return}
 const x=d.rules;
 $("photo").checked=!!x.require_photo;$("firstName").checked=!!x.require_first_name;
 $("lastName").checked=!!x.require_last_name;$("username").checked=!!x.require_username;
 $("admins").checked=!!x.ignore_admins;$("action").value=x.action||"review";
 $("dryRun").checked=!!x.dry_run;
}
async function saveRules(){
 const id=$("chatSelect").value;if(!id)return;
 const body={require_photo:$("photo").checked,require_first_name:$("firstName").checked,
 require_last_name:$("lastName").checked,require_username:$("username").checked,
 ignore_admins:$("admins").checked,action:$("action").value,dry_run:$("dryRun").checked};
 const r=await fetch("/api/dashboard/chats/"+id+"/rules",{method:"PUT",headers:headers(),body:JSON.stringify(body)});
 const d=await r.json();$("saveStatus").innerHTML=r.ok?'<span class="ok">✓ Configuração salva.</span>':'<span class="danger">'+(d.detail||"Erro ao salvar")+'</span>';
}
if(authToken){$("token").value=authToken;loadChats()}
</script>
</body>
</html>
"""
