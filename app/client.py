from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from .config import settings
from .database import get_supabase
from .dashboard import RulesUpdate

router = APIRouter()

def user_from_token(authorization):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sessão inválida.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        user = get_supabase().auth.get_user(token).user
        if not user:
            raise ValueError()
        return user
    except Exception:
        raise HTTPException(401, "Sessão expirada ou inválida.")

def workspace_for(db, uid):
    rows = db.table("workspace_members").select("workspace_id,role").eq("user_id", uid).limit(1).execute().data or []
    if rows:
        return rows[0]["workspace_id"], rows[0]["role"]
    ws = db.table("workspaces").insert({"name": "Meu Workspace", "owner_id": uid}).execute().data
    if not ws: raise HTTPException(500, "Não foi possível criar seu workspace.")
    wid = ws[0]["id"]
    db.table("workspace_members").insert({"workspace_id":wid,"user_id":uid,"role":"owner"}).execute()
    db.table("subscriptions").insert({"workspace_id":wid,"plan":"free","status":"active","max_groups":1,"max_users":100}).execute()
    return wid, "owner"

@router.get("/cliente", response_class=HTMLResponse)
async def client_page():
    return HTMLResponse(CLIENT_HTML.replace("__URL__", settings.supabase_url).replace("__KEY__", settings.supabase_publishable_key))

@router.get("/api/cliente/me")
async def client_me(authorization: str | None = Header(default=None)):
    u=user_from_token(authorization); db=get_supabase(); uid=str(u.id); wid,role=workspace_for(db,uid)
    p=db.table("profiles").select("id,full_name,role,status,created_at").eq("id",uid).limit(1).execute().data or []
    if not p:
        db.table("profiles").insert({"id":uid,"full_name":(getattr(u,"user_metadata",{}) or {}).get("full_name") or (u.email or "").split("@")[0],"role":"customer","status":"active"}).execute()
        p=db.table("profiles").select("id,full_name,role,status,created_at").eq("id",uid).limit(1).execute().data or []
    if p and p[0]["role"]=="admin": raise HTTPException(403,"Administradores devem usar o painel ADM.")
    s=db.table("subscriptions").select("plan,status,max_groups,max_users,current_period_end").eq("workspace_id",wid).limit(1).execute().data
    w=db.table("workspaces").select("id,name").eq("id",wid).limit(1).execute().data
    g=db.table("telegram_chats").select("id,title,telegram_chat_id,is_active").eq("workspace_id",wid).order("title").execute().data or []
    return {"user":{"id":uid,"email":u.email or "",**(p[0] if p else {})},"workspace":w[0] if w else None,"role":role,"subscription":s[0] if s else None,"groups":g}

def owned_chat(db,uid,chat_id):
    wid,_=workspace_for(db,uid)
    row=db.table("telegram_chats").select("id,title,telegram_chat_id,is_active").eq("id",chat_id).eq("workspace_id",wid).limit(1).execute().data or []
    if not row: raise HTTPException(404,"Grupo não encontrado.")
    return row[0]

@router.get("/api/cliente/grupos/{chat_id}")
async def group(chat_id:str,authorization:str|None=Header(default=None)):
    u=user_from_token(authorization);db=get_supabase(); owned_chat(db,str(u.id),chat_id)
    r=db.table("moderation_rules").select("require_photo,require_first_name,require_last_name,require_username,ignore_admins,action,dry_run,updated_at").eq("chat_id",chat_id).limit(1).execute().data
    scans=db.table("member_scans").select("violations,action_taken,scanned_at").eq("chat_id",chat_id).execute().data or []
    events=db.table("moderation_events").select("id,telegram_user_id,event_type,details,created_at").eq("chat_id",chat_id).order("created_at",desc=True).limit(50).execute().data or []
    return {"rules":r[0] if r else None,"stats":{"scans":len(scans),"violations":sum(bool(x.get("violations")) for x in scans),"restricts":sum(x.get("action_taken")=="restrict" for x in scans),"bans":sum(x.get("action_taken")=="ban" for x in scans)},"events":events}

@router.put("/api/cliente/grupos/{chat_id}/regras")
async def rules(chat_id:str,payload:RulesUpdate,authorization:str|None=Header(default=None)):
    u=user_from_token(authorization);db=get_supabase();owned_chat(db,str(u.id),chat_id)
    action="review" if payload.dry_run else payload.action
    db.table("moderation_rules").update({"require_photo":payload.require_photo,"require_first_name":payload.require_first_name,"require_last_name":payload.require_last_name,"require_username":payload.require_username,"ignore_admins":payload.ignore_admins,"action":action,"dry_run":payload.dry_run}).eq("chat_id",chat_id).execute()
    return {"ok":True}

@router.get("/api/cliente/grupos/{chat_id}/membros")
async def members(chat_id:str,authorization:str|None=Header(default=None),q:str=Query(default="")):
    u=user_from_token(authorization);db=get_supabase();owned_chat(db,str(u.id),chat_id)
    rows=db.table("member_scans").select("telegram_user_id,username,first_name,last_name,violations,action_taken,scanned_at").eq("chat_id",chat_id).order("scanned_at",desc=True).limit(200).execute().data or []
    q=q.strip().lower()
    if q: rows=[r for r in rows if q in str(r.get("telegram_user_id","")).lower() or q in str(r.get("username") or "").lower() or q in str(r.get("first_name") or "").lower() or q in str(r.get("last_name") or "").lower()]
    return {"members":rows}

CLIENT_HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SentinelChat — Cliente</title><script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script><style>
body{margin:0;background:linear-gradient(135deg,#07111f,#0b1b2e);color:#edf3fb;font-family:Arial}.client-banner{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;margin-bottom:16px;border:1px solid #1e4f78;border-radius:16px;background:linear-gradient(90deg,#0d2740,#10243a);box-shadow:0 10px 30px rgba(0,0,0,.22)}.client-banner strong{display:block;letter-spacing:.5px}.client-banner span{font-size:12px;color:#8fb5d8}.client-badge{padding:6px 10px;border-radius:999px;background:#0d6b9f;color:#d8f3ff!important;font-weight:800;font-size:11px!important}.wrap{max-width:1200px;margin:auto;padding:20px}.card{background:#101b2b;border:1px solid #293b53;border-radius:16px;padding:18px;margin:12px 0}.layout{display:grid;grid-template-columns:210px 1fr;gap:14px}.nav button{display:block;width:100%;padding:12px;margin:4px 0;background:transparent;color:#fff;border:0;text-align:left;border-radius:8px}.nav button.active{background:#1c2c42;border-left:3px solid #ff8a00}button{background:#ff8a00;border:0;border-radius:9px;padding:10px;font-weight:700}input,select{background:#0b1524;color:#fff;border:1px solid #354963;border-radius:8px;padding:10px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat{background:#0d1726;border:1px solid #293b53;border-radius:12px;padding:15px}.value{font-size:28px;font-weight:800}.muted{color:#91a2b8;font-size:13px}.section{display:none}.section.active{display:block}.row{display:flex;justify-content:space-between;gap:10px;padding:12px;background:#0d1726;border:1px solid #293b53;border-radius:9px;margin:7px 0}.rules{display:grid;grid-template-columns:1fr 1fr;gap:8px}.table{width:100%;border-collapse:collapse}.table td,.table th{padding:8px;border-bottom:1px solid #243349;text-align:left}@media(max-width:800px){.layout{grid-template-columns:1fr}.grid{grid-template-columns:1fr 1fr}}@media(max-width:500px){.grid,.rules{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><div class="client-banner"><div><strong>🛡️ SENTINELCHAT</strong><span>Área exclusiva do cliente</span></div><span class="client-badge">CLIENTE</span></div>
<div id="auth" class="card" style="max-width:430px;margin:70px auto"><div style="color:#6ec8ff;font-size:12px;font-weight:800;letter-spacing:1px">PAINEL DO CLIENTE</div><h1>🛡️ SentinelChat</h1><p class="muted">Acesso exclusivo do assinante</p><input id="email" placeholder="E-mail" style="width:100%;box-sizing:border-box;margin:5px 0"><input id="password" type="password" placeholder="Senha" style="width:100%;box-sizing:border-box;margin:5px 0"><input id="name" placeholder="Nome no cadastro" style="width:100%;box-sizing:border-box;margin:5px 0"><button onclick="signup()">Criar conta</button> <button onclick="login()">Entrar</button><p id="authmsg" class="muted"></p></div>
<div id="app" style="display:none"><div class="card" style="border-color:#1e4f78"><div style="display:flex;justify-content:space-between;align-items:center"><div><div style="font-size:12px;color:#6ec8ff;font-weight:800;letter-spacing:1px">MEU SENTINELCHAT</div><h1 style="margin:6px 0">🛡️ Painel do Cliente</h1><div id="hello" class="muted"></div></div><span class="client-badge">CLIENTE</span></div></div><div class="layout"><nav class="card nav"><button class="active" onclick="tab('home',this)">🏠 Visão geral</button><button onclick="tab('groups',this)">👥 Meus grupos</button><button onclick="tab('protection',this)">🛡️ Proteção</button><button onclick="tab('members',this)">👤 Membros</button><button onclick="tab('events',this)">🚨 Eventos</button><button onclick="tab('reports',this)">📊 Relatórios</button><button onclick="tab('subscription',this)">💳 Assinatura</button><button onclick="tab('account',this)">⚙️ Minha conta</button><button onclick="logout()">Sair</button></nav><main>
<section id="home" class="section active"><div class="grid"><div class="stat">Plano<div id="plan" class="value">—</div></div><div class="stat">Grupos<div id="gc" class="value">—</div></div><div class="stat">Violações<div id="vc" class="value">—</div></div><div class="stat">Banimentos<div id="bc" class="value">—</div></div></div><div class="card"><h2>Atividade recente</h2><div id="homeevents"></div></div></section>
<section id="groups" class="section"><div class="card"><h2>👥 Meus grupos</h2><div id="groupslist"></div></div></section>
<section id="protection" class="section"><div class="card"><h2>🛡️ Proteção</h2><select id="gsel" onchange="loadGroup()" style="width:100%"></select><div id="rules"></div></div></section>
<section id="members" class="section"><div class="card"><h2>👤 Membros</h2><input id="mq" placeholder="Pesquisar" oninput="loadMembers()" style="width:100%;box-sizing:border-box"><div id="membersbox"></div></div></section>
<section id="events" class="section"><div class="card"><h2>🚨 Eventos</h2><div id="eventsbox"></div></div></section>
<section id="reports" class="section"><div class="card"><h2>📊 Relatórios</h2><div id="reportsbox"></div></div></section>
<section id="subscription" class="section"><div class="card"><h2>💳 Minha assinatura</h2><div id="subbox"></div></div></section>
<section id="account" class="section"><div class="card"><h2>⚙️ Minha conta</h2><div id="accountbox"></div></div></section>
</main></div></div></div>
<script>
const sb=supabase.createClient("__URL__","__KEY__");let me=null;
const $=x=>document.getElementById(x),esc=x=>String(x??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
async function authHeaders(){const s=(await sb.auth.getSession()).data.session;return {"Authorization":"Bearer "+(s?.access_token||""),"Content-Type":"application/json"}}
async function api(u,o={}){const r=await fetch(u,{...o,headers:{...(o.headers||{}),...(await authHeaders())}});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||"Erro");return d}
async function signup(){
  const email=$("email").value.trim();
  const password=$("password").value;
  const name=$("name").value.trim();
  if(!email||!password||!name){$("authmsg").textContent="Preencha nome, e-mail e senha.";return;}
  if(password.length<6){$("authmsg").textContent="A senha precisa ter pelo menos 6 caracteres.";return;}
  $("authmsg").textContent="Criando conta...";
  const {data,error}=await sb.auth.signUp({
    email,
    password,
    options:{
      data:{full_name:name},
      emailRedirectTo:window.location.origin+"/cliente"
    }
  });
  if(error){$("authmsg").textContent="Não foi possível criar a conta: "+error.message;return;}
  if(data?.session){$("authmsg").textContent="Conta criada. Entrando...";await load();return;}
  $("authmsg").innerHTML="<b>Conta criada com sucesso.</b><br>Enviamos um e-mail de confirmação para <b>"+esc(email)+"</b>. Confirme o e-mail e depois clique em <b>Entrar</b>.";
}
async function login(){const {error}=await sb.auth.signInWithPassword({email:$("email").value.trim(),password:$("password").value});if(error){$("authmsg").textContent=error.message;return}load()}
async function logout(){await sb.auth.signOut();location.reload()}
function tab(id,b){document.querySelectorAll(".section").forEach(x=>x.classList.remove("active"));$(id).classList.add("active");document.querySelectorAll(".nav button").forEach(x=>x.classList.remove("active"));b.classList.add("active");if(id==="protection")loadGroup();if(id==="members")loadMembers();if(id==="events")loadGroup();if(id==="reports")loadGroup();if(id==="subscription")loadSub();if(id==="account")loadAccount()}
async function load(){try{me=await api("/api/cliente/me");$("auth").style.display="none";$("app").style.display="block";$("hello").textContent="Olá, "+(me.user.full_name||me.user.email)+" · "+me.user.email;$("plan").textContent=(me.subscription?.plan||"free").toUpperCase();$("gc").textContent=(me.groups||[]).filter(x=>x.is_active).length+" / "+(me.subscription?.max_groups||1);renderGroups();if(me.groups?.length){$("gsel").innerHTML=me.groups.map(g=>'<option value="'+g.id+'">'+esc(g.title||"Grupo")+"</option>").join("");}loadGroup();}catch(e){$("authmsg").textContent=e.message}}
function renderGroups(){
  const groups = me?.groups || [];
  if(!groups.length){
    $("groupslist").innerHTML="<p class='muted'>Nenhum grupo vinculado. Adicione o bot SentinelChat ao seu grupo e dê permissão de administrador.</p>";
    return;
  }
  $("groupslist").innerHTML=groups.map(g=>{
    const title=esc(g.title||"Grupo");
    const id=esc(g.id);
    const tg=esc(g.telegram_chat_id);
    const active=g.is_active ? "🟢 Ativo" : "🔴 Inativo";
    return "<div class='row'><span><b>"+title+"</b><br><span class='muted'>"+tg+" · "+active+"</span></span><button onclick='selectGroup(""+id+"")'>Configurar</button></div>";
  }).join("");
}
function selectGroup(id){
  $("gsel").value=id;
  const btn=document.querySelectorAll(".nav button")[2];
  tab("protection",btn);
}
async function loadGroup(){if(!me?.groups?.length)return;const id=$("gsel").value,d=await api("/api/cliente/grupos/"+id),x=d.rules||{};$("vc").textContent=d.stats.violations;$("bc").textContent=d.stats.bans;$("rules").innerHTML='<br><div class="rules"><label>Foto <input id="rp" type="checkbox" '+(x.require_photo?"checked":"")+'></label><label>Primeiro nome <input id="rf" type="checkbox" '+(x.require_first_name?"checked":"")+'></label><label>Sobrenome <input id="rl" type="checkbox" '+(x.require_last_name?"checked":"")+'></label><label>Username <input id="ru" type="checkbox" '+(x.require_username?"checked":"")+'></label><label>Ignorar admins <input id="ra" type="checkbox" '+(x.ignore_admins?"checked":"")+'></label></div><br><select id="act"><option value="review">Revisão</option><option value="restrict">Restringir</option><option value="ban">Banir</option></select> <label>DRY-RUN <input id="dry" type="checkbox" '+(x.dry_run?"checked":"")+'></label><br><br><button onclick="saveRules()">Salvar</button><p id="rmsg" class="muted"></p>';$("act").value=x.action||"review";$("homeevents").innerHTML=(d.events||[]).slice(0,8).map(e=>'<div class="row"><span><b>'+esc(e.event_type)+'</b></span><span class="muted">'+new Date(e.created_at).toLocaleString("pt-BR")+"</span></div>").join("")||"<p class=muted>Nenhuma atividade.</p>";$("eventsbox").innerHTML=$("homeevents").innerHTML;$("reportsbox").innerHTML='<div class="grid"><div class="stat">Análises<div class="value">'+d.stats.scans+'</div></div><div class="stat">Violações<div class="value">'+d.stats.violations+'</div></div><div class="stat">Restrições<div class="value">'+d.stats.restricts+'</div></div><div class="stat">Banimentos<div class="value">'+d.stats.bans+'</div></div></div>'}
async function saveRules(){const p={require_photo:$("rp").checked,require_first_name:$("rf").checked,require_last_name:$("rl").checked,require_username:$("ru").checked,ignore_admins:$("ra").checked,action:$("act").value,dry_run:$("dry").checked};try{await api("/api/cliente/grupos/"+$("gsel").value+"/regras",{method:"PUT",body:JSON.stringify(p)});$("rmsg").textContent="Regras salvas."}catch(e){$("rmsg").textContent=e.message}}
async function loadMembers(){if(!me?.groups?.length)return;const d=await api("/api/cliente/grupos/"+$("gsel").value+"/membros?q="+encodeURIComponent($("mq").value));$("membersbox").innerHTML=d.members.length?'<table class="table"><tr><th>Usuário</th><th>Username</th><th>Violações</th><th>Ação</th></tr>'+d.members.map(x=>"<tr><td>"+esc([x.first_name,x.last_name].filter(Boolean).join(" ")||"Sem nome")+"<br>"+x.telegram_user_id+"</td><td>"+esc(x.username?"@"+x.username:"—")+"</td><td>"+esc((x.violations||[]).join(", ")||"Nenhuma")+"</td><td>"+esc(x.action_taken||"OK")+"</td></tr>").join("")+"</table>":"<p class=muted>Nenhum membro.</p>"}
function loadSub(){const s=me.subscription||{};$("subbox").innerHTML="<h3>"+(s.plan||"free").toUpperCase()+" · "+(s.status||"active")+"</h3><p>Grupos: "+s.max_groups+" · Usuários: "+s.max_users+"</p><p>Vencimento: "+(s.current_period_end||"Não definido")+"</p><p class=muted>Checkout e pagamentos serão conectados nesta área.</p>"}
function loadAccount(){$("accountbox").innerHTML="<p><b>Nome:</b> "+esc(me.user.full_name||"—")+"</p><p><b>E-mail:</b> "+esc(me.user.email||"—")+"</p><p><b>Workspace:</b> "+esc(me.workspace?.name||"—")+"</p>"}
(async()=>{if((await sb.auth.getSession()).data.session)load()})();
</script></body></html>"""
