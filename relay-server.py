#!/usr/bin/env python3
"""
Print Relay Server v3 — 多用户版
用户自主注册(邮箱+密码) → 管理员审核 → 每人独立面板配对自己客户端
"""

import asyncio, json, struct, time, os, secrets, logging, hashlib
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock as TLock
from pathlib import Path

# ── Config ──────────────────────────────────────────────────
TCP_PORT = int(os.getenv("RELAY_TCP_PORT", "51900"))
HTTP_PORT = int(os.getenv("RELAY_HTTP_PORT", "51901"))
LOG_LEVEL = os.getenv("RELAY_LOG", "info").upper()
DATA_DIR = Path(os.getenv("RELAY_DATA", "/data"))

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
log = logging.getLogger('relay')

# ── Persistent state ────────────────────────────────────────
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

ADMIN_TOKEN = "90edba8f0283b2c1"

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return salt.hex() + ":" + h.hex()

def _check_password(password: str, stored: str) -> bool:
    salt_hex, h_hex = stored.split(":", 1)
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=64)
    return h.hex() == h_hex

def _new_account_token() -> str:
    return "tok_" + secrets.token_hex(8)

class State:
    def __init__(self):
        self.lock = TLock()
        # accounts[token] = {email, password_hash, name, status, limits, pairings, routes, history}
        self.accounts: dict[str, dict] = {}
        # 兼容旧数据
        self._migrated = False
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                # v3: accounts-based
                if 'accounts' in d:
                    self.accounts = d['accounts']
                    self._migrated = True
                else:
                    # v1/v2: migrate old globals into admin account
                    self._migrate(d)
                log.info(f"已加载: {len(self.accounts)} 账号")
            except Exception as e:
                log.warning(f"加载state失败: {e}, 初始化新状态")
                self._init_admin()
        else:
            self._init_admin()
            self.save()

    def _migrate(self, d: dict):
        """Migrate old v1/v2 pairings/routes into admin account."""
        self._init_admin()
        admin = self.accounts[ADMIN_TOKEN]
        admin['pairings'] = d.get('pairings', {})
        admin['routes'] = d.get('routes', [])
        admin['history'] = d.get('history', [])[:50]
        self._migrated = True
        log.info(f"已迁移旧数据: {len(admin['pairings'])} 配对, {len(admin['routes'])} 路由")

    def _init_admin(self):
        self.accounts[ADMIN_TOKEN] = {
            "email": "admin",
            "password_hash": "",
            "name": "管理员",
            "status": "active",
            "limits": {"max_clients": 999},
            "pairings": {},
            "routes": [],
            "history": [],
            "created": datetime.now(timezone.utc).isoformat()
        }

    def save(self):
        STATE_FILE.write_text(json.dumps({
            'accounts': self.accounts,
            'version': 3
        }, ensure_ascii=False, indent=2))

state = State()

# ── Async bridge ────────────────────────────────────────────
main_loop: asyncio.AbstractEventLoop | None = None
async_lock = asyncio.Lock()
clients: dict[str, dict] = {}

def run_async(coro):
    f = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return f.result(timeout=30)

# ── TCP Server ───────────────────────────────────────────────
async def handle_client(reader, writer):
    peer = writer.get_extra_info('peername')
    log.info(f"TCP 连接: {peer}")
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
    except Exception:
        writer.close(); return

    parts = line.decode().strip().split(maxsplit=1)
    if not parts:
        writer.close(); return

    cmd = parts[0].upper()

    # ── REGISTER <token> <name> ──
    if cmd == 'REGISTER' and len(parts) == 2:
        token = parts[1]
        # 查所有账号的 pairings
        found_account = None
        p = None
        with state.lock:
            for acct_tok, acct in state.accounts.items():
                if token in acct.get('pairings', {}):
                    found_account = acct_tok
                    p = acct['pairings'][token]
                    break

        if not p:
            writer.write(b"UNKNOWN_TOKEN\n"); await writer.drain(); writer.close(); return
        if p.get('type') != 'client':
            writer.write(b"WRONG_TYPE\n"); await writer.drain(); writer.close(); return

        client_name = p['name']

        # Receive JSON with printers
        try:
            jline = await asyncio.wait_for(reader.readline(), timeout=5)
            info = json.loads(jline.decode())
        except Exception:
            writer.write(b"BAD_JSON\n"); await writer.drain(); writer.close(); return

        width_mm = info.get('paper_width', 80)
        printers = info.get('printers', ['default'])

        # Check approval status
        with state.lock:
            acct = state.accounts.get(found_account, {})
            p = acct.get('pairings', {}).get(token, {})
            status = p.get('status', 'pending')
            if status != 'approved':
                writer.write(b"PENDING\n"); await writer.drain()
                while True:
                    await asyncio.sleep(2)
                    with state.lock:
                        a = state.accounts.get(found_account, {})
                        pp = a.get('pairings', {}).get(token, {})
                        if pp.get('status') == 'approved':
                            break
                writer.write(b"APPROVED\n")
            else:
                writer.write(b"APPROVED\n")
            await writer.drain()
            p['printers'] = printers
            p['width_mm'] = width_mm
            state.save()

        async with async_lock:
            clients[client_name] = {
                "writer": writer, "token": token, "printers": printers,
                "width_mm": width_mm,
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "account": found_account
            }
        log.info(f"客户端上线: {client_name} | 打印机: {printers}")

        try:
            sock = writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                try:
                    writer.write(b'\x00\x00\x00\x00')
                    await writer.drain()
                except Exception:
                    break

        hb_task = asyncio.create_task(heartbeat())

        try:
            while True:
                ln = await reader.readline()
                if not ln:
                    break
                line = ln.decode().strip()
                if line.startswith('{'):
                    try:
                        info = json.loads(line)
                        if 'printers' in info:
                            with state.lock:
                                a = state.accounts.get(found_account, {})
                                pp = a.get('pairings', {}).get(token, {})
                                pp['printers'] = info['printers']
                                if 'paper_width' in info:
                                    pp['width_mm'] = info['paper_width']
                                state.save()
                    except json.JSONDecodeError:
                        pass
        except asyncio.CancelledError:
            pass
        finally:
            hb_task.cancel()
            async with async_lock:
                clients.pop(client_name, None)
            log.info(f"客户端断开: {client_name}")
            writer.close()
        return

    writer.write(b"UNKNOWN_CMD\n"); await writer.drain()
    writer.close()

async def send_to_client(name, data):
    async with async_lock:
        info = clients.get(name)
    if not info:
        return False
    w = info["writer"]
    try:
        w.write(struct.pack('>I', len(data)) + data)
        await w.drain()
        return True
    except Exception:
        async with async_lock:
            clients.pop(name, None)
        return False

# ── HTTP Server ──────────────────────────────────────────────
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · 登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:#f0f2f5;color:#222;display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:#fff;border-radius:12px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:400px;width:100%}
h1{font-size:22px;text-align:center;margin-bottom:4px}.sub{text-align:center;color:#888;font-size:13px;margin-bottom:24px}
label{display:block;font-size:12px;color:#666;margin-bottom:4px}
input{width:100%;padding:10px 12px;border:1px solid #ccc;border-radius:8px;font-size:14px;margin-bottom:12px}
input:focus{outline:none;border-color:#1976d2}
.btn{width:100%;padding:11px;background:#1976d2;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-weight:500}
.btn:hover{background:#1565c0}
.btn.ghost{background:none;color:#1976d2;margin-top:8px;font-size:13px}
.msg{font-size:12px;text-align:center;margin-top:8px}.msg.err{color:#c62828}.msg.ok{color:#2e7d32}
.tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #eee}
.tab{flex:1;text-align:center;padding:10px;cursor:pointer;font-weight:500;color:#888;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab.active{color:#1976d2;border-bottom-color:#1976d2}
</style></head><body>
<div class="card">
<h1>🖨️ Print Relay</h1>
<div class="sub">打印管理中心</div>
<div class="tabs">
  <div class="tab active" onclick="showTab('login')">登录</div>
  <div class="tab" onclick="showTab('register')">注册</div>
</div>
<div id="form-login">
  <label>邮箱</label><input id="l_email" type="email" placeholder="chef@example.com">
  <label>密码</label><input id="l_password" type="password" placeholder="••••••">
  <button class="btn" onclick="doLogin()">登录</button>
</div>
<div id="form-register" style="display:none">
  <label>邮箱</label><input id="r_email" type="email" placeholder="chef@example.com">
  <label>密码</label><input id="r_password" type="password" placeholder="最少6位">
  <button class="btn" onclick="doRegister()">注册</button>
</div>
<div id="msg" class="msg"></div>
</div>
<script>
function showTab(t){
 document.querySelectorAll('.tab').forEach((e,i)=>e.classList.toggle('active',i==(t==='login'?0:1)));
 document.getElementById('form-login').style.display=t==='login'?'block':'none';
 document.getElementById('form-register').style.display=t==='register'?'block':'none';
 document.getElementById('msg').textContent='';
}
async function doLogin(){
 let e=document.getElementById('l_email').value.trim();
 let p=document.getElementById('l_password').value;
 if(!e||!p){msg('请输入邮箱和密码','err');return}
 let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e,password:p})});
 let d=await r.json();
 if(r.ok){msg('✅ 登录成功，跳转中...','ok');setTimeout(()=>location.href='/panel?token='+d.token,500)}
 else msg(d.error||'登录失败','err');
}
async function doRegister(){
 let e=document.getElementById('r_email').value.trim();
 let p=document.getElementById('r_password').value;
 if(!e||p.length<6){msg('密码至少6位','err');return}
 let r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e,password:p})});
 let d=await r.json();
 if(r.ok) msg('✅ 注册成功！等待管理员审核','ok');
 else msg(d.error||'注册失败','err');
}
function msg(t,c){ let m=document.getElementById('msg'); m.textContent=t; m.className='msg '+c }
</script>
</body></html>"""

PANEL_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · 打印管理中心</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:#f0f2f5;color:#222;padding:20px;max-width:960px;margin:0 auto}
h1{font-size:22px}.sub{color:#888;font-size:12px;margin-bottom:18px}
.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card h2{font-size:15px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}.col{flex:1;min-width:160px}
.col label{display:block;font-size:12px;color:#666;margin-bottom:3px}
input,select{width:100%;padding:7px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px}
input:focus,select:focus{outline:none;border-color:#1976d2}
.btn{padding:7px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;white-space:nowrap}
.btn.primary{background:#1976d2;color:#fff}.btn.primary:hover{background:#1565c0}
.btn.success{background:#2e7d32;color:#fff}.btn.success:hover{background:#1b5e20}
.btn.danger{background:#c62828;color:#fff}.btn.danger:hover{background:#b71c1c}
.btn.small{padding:4px 10px;font-size:11px}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.tag.woo{background:#e3f2fd;color:#1565c0}.tag.client{background:#e8f5e9;color:#2e7d32}
.tag.pending{background:#fff3e0;color:#e65100}.tag.active{background:#e8f5e9;color:#2e7d32}.tag.suspended{background:#ffebee;color:#c62828}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #eee}
th{color:#888;font-weight:500;font-size:11px}
.msg{font-size:12px;margin-top:4px}.msg.ok{color:#2e7d32}.msg.err{color:#c62828}
.empty{color:#999;font-size:13px}
.logout{float:right;color:#888;font-size:12px;text-decoration:none;margin-top:4px}
</style></head><body>
<h1>🖨️ Print Relay <span style="font-size:14px;color:#888" id="acct_name"></span></h1>
<div class="sub"><span id="acct_email"></span> · 客户端 <span id="acct_clients">0/0</span></div>

<!-- ═══ 配对管理 ═══ -->
<div class="card">
<h2>📡 已配对设备</h2>
<div id="pairings" class="empty">加载中...</div>
<div class="row" style="margin-top:12px;align-items:stretch">
  <div class="col"><input id="new_tok" placeholder="粘贴 Token（从 WP 插件或客户端复制）"></div>
  <div class="col" style="min-width:100px">
    <select id="new_type"><option value="woo">🛒 WooCommerce</option><option value="shopify">🛍️ Shopify</option><option value="pos">🏪 POS 系统</option><option value="custom">🔧 自定义 API</option><option value="client">💻 客户端 PC</option></select>
  </div>
  <div class="col"><input id="new_name" placeholder="名称（如 thecarte.eu）"></div>
  <div><button class="btn primary" onclick="addPairing()" style="height:37px">➕ 添加配对</button></div>
</div>
<div id="pair_msg" class="msg"></div>
</div>

<!-- ═══ 路由配置 ═══ -->
<div class="card">
<h2>🔀 打印路由</h2>
<div id="routes" class="empty">加载中...</div>
<div class="row" style="margin-top:12px">
  <div class="col"><label>订单来源</label><select id="rt_woo"><option value="">— 先添加 WooCommerce 配对 —</option></select></div>
  <div class="col"><label>目标客户端</label><select id="rt_client"><option value="">— 先添加客户端配对 —</option></select></div>
  <div class="col"><label>打印机</label><select id="rt_printer"><option value="">—</option></select></div>
  <div><label>&nbsp;</label><button class="btn success" onclick="addRoute()">+ 添加路由</button></div>
</div>
<div id="route_msg" class="msg"></div>
</div>

<!-- ═══ 打印历史 ═══ -->
<div class="card">
<h2>📋 打印历史</h2>
<div id="history" class="empty">加载中...</div>
</div>

<script>
const T = new URLSearchParams(location.search).get('token') || '';
if(!T){location.href='/login';}

async function api(p,o={}){
 let sep = p.includes('?')?'&':'?';
 let r = await fetch(p+sep+'token='+T,o);
 return r.json();
}

async function refresh(){
 let d = await api('/api/pairings');
 document.getElementById('acct_name').textContent = d.account_name||'';
 document.getElementById('acct_email').textContent = d.account_email||'';
 document.getElementById('acct_clients').textContent = (d.client_count||0)+'/'+(d.max_clients||3);

 let pairings = d.pairings||{};
 let ph='';
 if(Object.keys(pairings).length===0){
  ph='<span class="empty">暂无配对 — 在上方输入框粘贴 Token 添加</span>';
 }else{
  ph='<table><tr><th>类型</th><th>名称</th><th>Token</th><th>状态</th><th>打印机</th><th>操作</th></tr>';
  for(let [tok,p] of Object.entries(pairings)){
   let labels={woo:'WooCommerce',shopify:'Shopify',pos:'POS系统',custom:'自定义API',client:'客户端'};
   let icons={woo:'🛒',shopify:'🛍️',pos:'🏪',custom:'🔧',client:'💻'};
   let cls=p.type==='client'?'client':'woo';
   let icon=icons[p.type]||'📡';
   let statusLabel={'pending':'⏳待审','approved':'✅已批','rejected':'❌拒绝'}[p.status]||p.status;
   let printers=p.printers?p.printers.join(', '):(p.type==='client'?'等待上线...':'—');
   ph+=`<tr><td><span class="tag ${cls}">${icon} ${labels[p.type]||p.type}</span></td><td>${p.name}</td><td><code>${tok}</code></td><td>${statusLabel}</td><td style="font-size:12px;color:#666">${printers}</td><td><button class="btn danger small" onclick="delPairing('${tok}')">删除</button></td></tr>`;
  }
  ph+='</table>';
 }
 document.getElementById('pairings').innerHTML=ph;

 let sources=Object.entries(pairings).filter(([t,p])=>p.type!=='client');
 let cls=Object.entries(pairings).filter(([t,p])=>p.type==='client');
 let ws=document.getElementById('rt_woo'), ws_val=ws.value;
 let cs=document.getElementById('rt_client'), cs_val=cs.value;
 let ps=document.getElementById('rt_printer'), ps_val=ps.value;

 ws.innerHTML='<option value="">— 选择来源 —</option>';
 sources.forEach(([t,p])=>ws.add(new Option(p.name+' ('+t+')',t)));
 ws.value=ws_val;

 cs.innerHTML='<option value="">— 选择客户端 —</option>';
 cls.forEach(([t,p])=>cs.add(new Option(p.name,p.name)));
 cs.value=cs_val;

 ps.innerHTML='<option value="">— 默认 —</option>';
 if(cs_val){
  let cl=cls.find(x=>x[1].name===cs_val);
  if(cl&&cl[1].printers) cl[1].printers.forEach(p=>ps.add(new Option(p,p)));
  ps.value=ps_val;
 }

 document.getElementById('rt_client').onchange=function(){
  let cn=this.value;
  let ps2=document.getElementById('rt_printer');
  ps2.innerHTML='<option value="">— 默认 —</option>';
  let cl2=cls.find(x=>x[1].name===cn);
  if(cl2&&cl2[1].printers) cl2[1].printers.forEach(p=>ps2.add(new Option(p,p)));
 };

 let rd=await api('/api/routes');
 let rh='';
 if(!rd.routes||rd.routes.length===0){
  rh='<span class="empty">暂无路由 — 请先添加 WooCommerce 和客户端配对</span>';
 }else{
  rh='<table><tr><th>订单来源</th><th>→ 客户端</th><th>→ 打印机</th><th></th></tr>';
  rd.routes.forEach((r,i)=>{
   let wn=(pairings[r.woo_token])?pairings[r.woo_token].name:r.woo_token;
   rh+=`<tr><td>${wn}</td><td>${r.client||''}</td><td>${r.printer||'默认'}</td><td><button class="btn danger small" onclick="delRoute(${i})">删除</button></td></tr>`;
  });
  rh+='</table>';
 }
 document.getElementById('routes').innerHTML=rh;

 let hd=await api('/api/history');
 let hh='';
 if(!hd.history||hd.history.length===0) hh='暂无记录';
 else{
  hh='<table><tr><th>时间</th><th>客户端</th><th>打印机</th><th>订单</th></tr>';
  hd.history.slice(0,20).forEach(e=>hh+=`<tr><td>${(e.time||'').slice(11,19)}</td><td>${e.client}</td><td>${e.printer}</td><td>#${e.order} (${e.items}项)</td></tr>`);
  hh+='</table>';
 }
 document.getElementById('history').innerHTML=hh;
}

async function addPairing(){
 let tok=document.getElementById('new_tok').value.trim();
 if(!tok||tok.length<4){document.getElementById('pair_msg').className='msg err';document.getElementById('pair_msg').textContent='Token 太短';return}
 let type=document.getElementById('new_type').value;
 let name=document.getElementById('new_name').value.trim()||(type==='woo'?'WooCommerce':'客户端');
 let myPairings = (await api('/api/pairings')).pairings||{};
 let clientCount = Object.values(myPairings).filter(p=>p.type==='client').length;
 let maxClients = (await api('/api/pairings')).max_clients||3;
 if(type==='client' && clientCount >= maxClients){
  document.getElementById('pair_msg').className='msg err';
  document.getElementById('pair_msg').textContent='❌ 已达客户端上限 ('+maxClients+')';
  return;
 }
 let r=await fetch('/api/pairings?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok,type:type,name:name,status:'approved'})});
 if(r.ok){
  document.getElementById('new_tok').value='';document.getElementById('new_name').value='';
  document.getElementById('pair_msg').className='msg ok';document.getElementById('pair_msg').textContent='✅ 已添加';
 }else{
  let d=await r.json();
  document.getElementById('pair_msg').className='msg err';
  document.getElementById('pair_msg').textContent='❌ '+(d.error||'添加失败');
 }
 refresh();
}

async function delPairing(tok){
 if(!confirm('确认删除配对 '+tok+'？'))return;
 await fetch('/api/forget?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
 refresh();
}

async function addRoute(){
 let woo=document.getElementById('rt_woo').value;
 let client=document.getElementById('rt_client').value;
 let printer=document.getElementById('rt_printer').value;
 if(!woo||!client){document.getElementById('route_msg').className='msg err';document.getElementById('route_msg').textContent='请选择来源和客户端';return}
 let r=await fetch('/api/routes?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({woo_token:woo,client:client,printer:printer})});
 if(r.ok){
  document.getElementById('route_msg').className='msg ok';document.getElementById('route_msg').textContent='✅ 路由已添加';
 }else{
  document.getElementById('route_msg').className='msg err';document.getElementById('route_msg').textContent='❌ 失败';
 }
 refresh();
}

async function delRoute(i){
 await api('/api/routes/'+i,{method:'DELETE'});
 refresh();
}

refresh();setInterval(refresh,10000);
</script></body></html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · 管理员</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:#f0f2f5;color:#222;padding:20px;max-width:960px;margin:0 auto}
h1{font-size:22px;margin-bottom:18px}
.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card h2{font-size:15px;margin-bottom:10px}
.btn{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:500;margin-right:4px}
.btn.approve{background:#2e7d32;color:#fff}.btn.reject{background:#e65100;color:#fff}.btn.suspend{background:#c62828;color:#fff}.btn.activate{background:#1976d2;color:#fff}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.tag.pending{background:#fff3e0;color:#e65100}.tag.active{background:#e8f5e9;color:#2e7d32}.tag.suspended{background:#ffebee;color:#c62828}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #eee}
th{color:#888;font-weight:500;font-size:11px}
.limits input{width:40px;padding:2px 4px;border:1px solid #ccc;border-radius:4px;font-size:12px;text-align:center}
.msg{font-size:12px;margin-top:4px}.msg.ok{color:#2e7d32}
</style></head><body>
<h1>🔐 Print Relay · 管理员</h1>

<div class="card">
<h2>📋 待审核</h2>
<div id="pending" class="empty">加载中...</div>
</div>

<div class="card">
<h2>👥 全部账号</h2>
<div id="all_accounts" class="empty">加载中...</div>
<div id="admin_msg" class="msg"></div>
</div>

<script>
const T=new URLSearchParams(location.search).get('token')||'';

async function api(p,o={}){
 let sep=p.includes('?')?'&':'?';
 let r=await fetch(p+sep+'token='+T,o);
 return r.json();
}

async function refresh(){
 let d=await api('/admin/accounts');
 let pending='',all='';
 if(!d.accounts||Object.keys(d.accounts).length===0){
  pending='暂无';all='暂无';
 }else{
  let pcount=0;
  for(let [tok,a] of Object.entries(d.accounts)){
   let statusTag='<span class="tag '+a.status+'">'+a.status+'</span>';
   let clients=Object.values(a.pairings||{}).filter(p=>p.type==='client').length;
   let max=a.limits?.max_clients||3;

   let actions='';
   if(a.status==='pending'){
    actions='<button class="btn approve" onclick="approve(\''+tok+'\')">批准</button><button class="btn reject" onclick="reject(\''+tok+'\')">拒绝</button>';
    pcount++;
   }else if(a.status==='active'){
    actions='<button class="btn suspend" onclick="setStatus(\''+tok+'\',\'suspended\')">暂停</button>';
   }else if(a.status==='suspended'){
    actions='<button class="btn activate" onclick="setStatus(\''+tok+'\',\'active\')">恢复</button>';
   }
   actions+='<span class="limits"> 上限 <input id="lim_'+tok+'" value="'+max+'" size="2"> <button class="btn activate" onclick="setLimit(\''+tok+'\')">保存</button></span>';

   if(a.status==='pending'){
    pending+=`<tr><td>${a.email}</td><td>${a.name||'—'}</td><td>${a.created?.slice(0,10)||''}</td><td>${actions}</td></tr>`;
   }
   all+=`<tr><td>${a.email}</td><td>${a.name||'—'}</td><td>${statusTag}</td><td>${clients}/${max}</td><td>${a.created?.slice(0,16)||''}</td><td>${actions}</td></tr>`;
  }
  pending=(pcount===0?'<span class="empty">无待审核</span>':'<table><tr><th>邮箱</th><th>名称</th><th>注册时间</th><th>操作</th></tr>')+pending+(pcount>0?'</table>':'');
  all='<table><tr><th>邮箱</th><th>名称</th><th>状态</th><th>客户端</th><th>注册时间</th><th>操作</th></tr>'+all+'</table>';
 }
 document.getElementById('pending').innerHTML=pending;
 document.getElementById('all_accounts').innerHTML=all;
}

async function approve(tok){await fetch('/admin/approve?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});refresh()}
async function reject(tok){await fetch('/admin/reject?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});refresh()}
async function setStatus(tok,s){await fetch('/admin/status?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok,status:s})});refresh()}
async function setLimit(tok){
 let v=parseInt(document.getElementById('lim_'+tok).value)||3;
 await fetch('/admin/limits?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok,max_clients:v})});
 document.getElementById('admin_msg').className='msg ok';document.getElementById('admin_msg').textContent='✅ 已更新';
 refresh();
}

refresh();setInterval(refresh,15000);
</script></body></html>"""

REGISTER_PAGE_SIMPLE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · Token</title>
<style>
body{font:14px/1.5 system-ui;background:#f5f5f5;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:10px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);max-width:400px;text-align:center}
h1{font-size:20px;margin-bottom:8px}.sub{color:#666;font-size:13px;margin-bottom:20px}
.code{font:bold 32px monospace;background:#e3f2fd;padding:12px 24px;border-radius:8px;letter-spacing:3px;margin:16px 0;display:inline-block}
.help{font-size:12px;color:#999;margin-top:16px}
.btn{padding:8px 20px;background:#1976d2;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;margin-top:12px}
</style></head><body>
<div class="card">
<h1>🖨️ Print Relay</h1>
<div class="sub">Ihr Registrierungs-Token:</div>
<div class="code">__TOKEN__</div>
<p class="help">Kopieren und im Control Panel einfugen.</p>
<a href="/login" class="btn">Zum Login</a>
</div>
</body></html>"""

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f"HTTP {args}")

    def _get_token(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        return qs.get('token', [''])[0]

    def _get_account(self, token: str):
        """返回 (account_token, account_dict) 或 (None, None)"""
        if not token:
            return None, None
        with state.lock:
            if token in state.accounts:
                return token, state.accounts[token]
        return None, None

    def _is_admin(self, token: str) -> bool:
        return token == ADMIN_TOKEN

    def _resolve_token_auth(self, owner_token: str, pairing_token: str) -> bool:
        """检查 pairing_token 是否属于 owner_token 的账号"""
        with state.lock:
            acct = state.accounts.get(owner_token, {})
            return pairing_token in acct.get('pairings', {})

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        token = self._get_token()

        if path == '/health':
            self._json({'ok': True}); return

        if path == '/login':
            self._html(LOGIN_PAGE); return

        if path == '/register':
            # 旧版注册（给 WP 插件生成 token 用）
            tok = secrets.token_hex(4)
            with state.lock:
                admin = state.accounts.get(ADMIN_TOKEN, {})
                admin_pairings = admin.get('pairings', {})
                admin_pairings[tok] = {"type": "woo", "name": f"Woo-{tok[:4]}", "status": "pending", "created_at": datetime.now(timezone.utc).isoformat()}
                state.save()
            html = REGISTER_PAGE_SIMPLE.replace('__TOKEN__', tok)
            self._html(html); return

        # ── 管理员面板 ──
        if path == '/admin' and self._is_admin(token):
            self._html(ADMIN_HTML); return

        if path == '/admin/accounts' and self._is_admin(token):
            with state.lock:
                self._json({'accounts': state.accounts}); return

        # ── 用户面板 ──
        acct_tok, acct = self._get_account(token)
        if not acct:
            if path in ('/', '/panel'):
                self._html(LOGIN_PAGE); return
            self._json({'error': 'unauthorized'}, 403); return

        if path in ('/', '/panel'):
            self._html(PANEL_HTML); return

        if path == '/api/pairings':
            pairings = acct.get('pairings', {})
            client_count = sum(1 for p in pairings.values() if p.get('type') == 'client')
            self._json({
                'pairings': pairings,
                'account_name': acct.get('name', ''),
                'account_email': acct.get('email', ''),
                'client_count': client_count,
                'max_clients': acct.get('limits', {}).get('max_clients', 3)
            }); return

        if path == '/api/routes':
            self._json({'routes': acct.get('routes', [])}); return

        if path == '/api/history':
            self._json({'history': acct.get('history', [])}); return

        self.send_error(404)

    def do_POST(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        token = self._get_token()
        cl = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(cl)) if cl else {}

        # ── 注册 ──
        if path == '/api/register':
            email = body.get('email', '').strip()
            password = body.get('password', '')
            if not email or len(password) < 6:
                self._json({'error': '邮箱不能为空，密码至少6位'}, 400); return

            with state.lock:
                # 查重复
                for acct in state.accounts.values():
                    if acct.get('email') == email:
                        self._json({'error': '该邮箱已注册'}, 409); return

                acct_token = _new_account_token()
                state.accounts[acct_token] = {
                    "email": email,
                    "password_hash": _hash_password(password),
                    "name": "",
                    "status": "pending",
                    "limits": {"max_clients": 3},
                    "pairings": {},
                    "routes": [],
                    "history": [],
                    "created": datetime.now(timezone.utc).isoformat()
                }
                state.save()
            log.info(f"新注册: {email} → {acct_token}")
            self._json({'ok': True}); return

        # ── 登录 ──
        if path == '/api/login':
            email = body.get('email', '').strip()
            password = body.get('password', '')
            # 管理员快捷登录
            if email == 'admin' and password == ADMIN_TOKEN:
                self._json({'token': ADMIN_TOKEN}); return

            with state.lock:
                for acct_tok, acct in state.accounts.items():
                    if acct.get('email') == email:
                        if acct.get('status') == 'suspended':
                            self._json({'error': '账号已被暂停'}, 403); return
                        if _check_password(password, acct['password_hash']):
                            self._json({'token': acct_tok}); return
                        self._json({'error': '密码错误'}, 401); return
            self._json({'error': '邮箱未注册'}, 404); return

        # ── WooCommerce webhook ──
        if path.startswith('/wc'):
            # token 是 account 的，还是 pairings 的？
            woo_token = token  # 从 URL ?token=xxx 来的
            # 找这个 token 属于哪个 pairing (在所有账号中)
            found_acct = None
            with state.lock:
                for atok, acct in state.accounts.items():
                    if woo_token in acct.get('pairings', {}):
                        found_acct = atok
                        break

            if not found_acct:
                self._json({'error': 'unauthorized'}, 403); return

            target_client = self.headers.get('X-Print-Client', '')
            target_printer = self.headers.get('X-Printer-Name', '')
            cut_per_item = self.headers.get('X-Cut-Per-Item', '') == '1'

            if not target_client:
                order = body
                if cut_per_item:
                    order = {**order, 'cut_per_item': True}
                with state.lock:
                    acct = state.accounts[found_acct]
                    routes = acct.get('routes', [])
                matched = [r for r in routes if r.get('woo_token') == woo_token]
                if not matched:
                    log.warning(f"POST /wc: token={woo_token} no routes")
                    self._json({'error': 'No route configured'}, 503); return

                results = []
                for r in matched:
                    cli = r.get('client', '')
                    prn = r.get('printer', '')
                    payload = json.dumps({"type": "print", "order": order, "printer": prn}).encode()
                    ok = run_async(send_to_client(cli, payload))
                    results.append({'client': cli, 'printer': prn, 'ok': ok})
                    if ok:
                        with state.lock:
                            a = state.accounts[found_acct]
                            a.setdefault('history', []).insert(0, {
                                "time": datetime.now(timezone.utc).isoformat(),
                                "client": cli, "printer": prn or "default",
                                "order": str(order.get('number', order.get('id'))),
                                "items": len(order.get('line_items', []))
                            })
                            if len(a['history']) > 50: a['history'].pop()

                with state.lock: state.save()
                self._json({'status': 'ok', 'results': results}); return
            else:
                order = {**body}
                if cut_per_item: order['cut_per_item'] = True
                payload = json.dumps({"type": "print", "order": order, "printer": target_printer}).encode()
                ok = run_async(send_to_client(target_client, payload))
                results = [{'client': target_client, 'printer': target_printer, 'ok': ok}]
                if ok:
                    with state.lock:
                        a = state.accounts[found_acct]
                        a.setdefault('history', []).insert(0, {
                            "time": datetime.now(timezone.utc).isoformat(),
                            "client": target_client, "printer": target_printer or "default",
                            "order": str(body.get('number', 'TEST')),
                            "items": len(body.get('line_items', []))
                        })
                        if len(a['history']) > 50: a['history'].pop()
                with state.lock: state.save()
                self._json({'status': 'ok', 'results': results}); return

        # ── 管理员操作 ──
        if self._is_admin(token):
            if path == '/admin/approve':
                u_tok = body.get('token', '')
                with state.lock:
                    if u_tok in state.accounts:
                        state.accounts[u_tok]['status'] = 'active'
                        state.save()
                self._json({'ok': True}); return

            if path == '/admin/reject':
                u_tok = body.get('token', '')
                with state.lock:
                    if u_tok in state.accounts:
                        state.accounts[u_tok]['status'] = 'rejected'
                        state.save()
                self._json({'ok': True}); return

            if path == '/admin/status':
                u_tok = body.get('token', '')
                new_status = body.get('status', '')
                with state.lock:
                    if u_tok in state.accounts and new_status in ('active', 'suspended'):
                        state.accounts[u_tok]['status'] = new_status
                        state.save()
                self._json({'ok': True}); return

            if path == '/admin/limits':
                u_tok = body.get('token', '')
                mx = body.get('max_clients')
                with state.lock:
                    if u_tok in state.accounts and isinstance(mx, int) and mx > 0:
                        state.accounts[u_tok].setdefault('limits', {})['max_clients'] = mx
                        state.save()
                self._json({'ok': True}); return

        # ── 用户 API (需要 account token) ──
        acct_tok, acct = self._get_account(token)
        if not acct:
            self._json({'error': 'unauthorized'}, 403); return

        if path == '/api/approve' and acct.get('status') == 'active':
            ptok = body.get('token', '')
            with state.lock:
                if ptok in acct.get('pairings', {}):
                    acct['pairings'][ptok]['status'] = 'approved'
                    state.save()
            self._json({'ok': True}); return

        if path == '/api/reject':
            ptok = body.get('token', '')
            with state.lock:
                if ptok in acct.get('pairings', {}):
                    acct['pairings'][ptok]['status'] = 'rejected'
                    state.save()
            self._json({'ok': True}); return

        if path == '/api/forget':
            ptok = body.get('token', '')
            with state.lock:
                acct['pairings'].pop(ptok, None)
                # 同时删相关路由
                acct['routes'] = [r for r in acct.get('routes', []) if r.get('woo_token') != ptok and r.get('client') != ptok]
                state.save()
            self._json({'ok': True}); return

        if path == '/api/routes':
            with state.lock:
                acct.setdefault('routes', []).append({
                    "woo_token": body.get('woo_token', ''),
                    "client": body.get('client', ''),
                    "printer": body.get('printer', ''),
                })
                state.save()
            self._json({'ok': True}); return

        if path == '/api/pairings':
            ptok = body.get('token', '')
            ptype = body.get('type', 'woo')
            if not ptok or len(ptok) < 4:
                self._json({'error': 'Token too short'}, 400); return

            # 客户端上限检查
            pairings = acct.get('pairings', {})
            if ptype == 'client':
                client_count = sum(1 for p in pairings.values() if p.get('type') == 'client')
                max_clients = acct.get('limits', {}).get('max_clients', 3)
                if client_count >= max_clients:
                    self._json({'error': f'已达客户端上限 ({max_clients})'}, 403); return

            with state.lock:
                acct.setdefault('pairings', {})[ptok] = {
                    "type": ptype,
                    "name": body.get('name', f"{ptype}-{ptok[:4]}"),
                    "status": "approved",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
                state.save()
            self._json({'ok': True, 'token': ptok}); return

        if path == '/api/register':
            # Plugin auto-register (WP plugin POSTs token to register)
            ptok = body.get('token', '')
            ptype = body.get('type', 'woo')
            name = body.get('name', f"{ptype}-{ptok[:4]}")
            with state.lock:
                if ptok not in acct.get('pairings', {}):
                    acct.setdefault('pairings', {})[ptok] = {
                        "type": ptype, "name": name, "status": "approved",
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }
                    state.save()
            self._json({'ok': True, 'token': ptok}); return

        self.send_error(404)

    def do_DELETE(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        token = self._get_token()

        if path.startswith('/api/routes/') and token:
            idx = int(path.split('/')[-1])
            acct_tok, acct = self._get_account(token)
            if not acct:
                self._json({'error': 'unauthorized'}, 403); return
            with state.lock:
                routes = acct.get('routes', [])
                if 0 <= idx < len(routes):
                    routes.pop(idx)
                    state.save()
            self._json({'ok': True}); return

        self.send_error(404)

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html(self, html, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode())

def run_http():
    server = HTTPServer(('0.0.0.0', HTTP_PORT), WebhookHandler)
    log.info(f"HTTP 监听 :{HTTP_PORT}")
    server.serve_forever()

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    tcp = await asyncio.start_server(handle_client, '0.0.0.0', TCP_PORT)
    log.info(f"TCP 监听 :{TCP_PORT}")
    Thread(target=run_http, daemon=True).start()
    async with tcp:
        await tcp.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
