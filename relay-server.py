#!/usr/bin/env python3
"""
Print Relay Server v2 — 配对制管理中心
客户端/WooCommerce 各持随机 Token → 面板审批配对 → 建立路由
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

class State:
    def __init__(self):
        self.lock = TLock()
        self.pairings: dict[str, dict] = {}   # token → {type, name, status, printers, ...}
        self.routes: list[dict] = []           # [{woo_token, client_token, printer}]
        self.history: list[dict] = []          # print history
        self.panel_token: str = os.getenv("PANEL_TOKEN", "90edba8f0283b2c1")  # admin access
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                self.pairings = d.get('pairings', {})
                self.routes = d.get('routes', [])
                self.history = d.get('history', [])[:50]
                self.panel_token = d.get('panel_token', self.panel_token)
                log.info(f"已加载状态: {len(self.pairings)} 配对, {len(self.routes)} 路由")
            except: pass
        else:
            self.save()
        log.info(f"🔑 控制面板 Token: {self.panel_token}")

    def save(self):
        STATE_FILE.write_text(json.dumps({
            'pairings': self.pairings,
            'routes': self.routes,
            'history': self.history[:50],
            'panel_token': self.panel_token,
        }, ensure_ascii=False, indent=2))

state = State()

# ── Async bridge ────────────────────────────────────────────
main_loop: asyncio.AbstractEventLoop | None = None
async_lock = asyncio.Lock()
# clients[name] = {"writer": ..., "token": ..., "printers": [...], "width_mm": 80}
clients: dict[str, dict] = {}

def run_async(coro):
    f = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return f.result(timeout=30)

# ── JSON order forward (client renders with Jinja2) ──────────


# ── TCP Server ───────────────────────────────────────────────
async def handle_client(reader, writer):
    peer = writer.get_extra_info('peername')
    log.info(f"TCP 连接: {peer}")
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
    except:
        writer.close(); return

    parts = line.decode().strip().split(maxsplit=1)
    if not parts:
        writer.close(); return

    cmd = parts[0].upper()

    # ── REGISTER <token> <name> ──
    if cmd == 'REGISTER' and len(parts) == 2:
        token = parts[1]
        with state.lock:
            if token not in state.pairings:
                writer.write(b"UNKNOWN_TOKEN\n"); await writer.drain(); writer.close(); return
            p = state.pairings[token]
            if p.get('type') != 'client':
                writer.write(b"WRONG_TYPE\n"); await writer.drain(); writer.close(); return
            client_name = p['name']

        # Receive JSON with printers
        try:
            jline = await asyncio.wait_for(reader.readline(), timeout=5)
            info = json.loads(jline.decode())
        except:
            writer.write(b"BAD_JSON\n"); await writer.drain(); writer.close(); return

        width_mm = info.get('paper_width', 80)
        printers = info.get('printers', ['default'])

        # Check approval status
        with state.lock:
            p = state.pairings[token]
            status = p.get('status', 'pending')
            if status != 'approved':
                writer.write(b"PENDING\n"); await writer.drain()
                # Wait for approval (poll)
                while True:
                    await asyncio.sleep(2)
                    with state.lock:
                        if state.pairings[token].get('status') == 'approved':
                            break
                writer.write(b"APPROVED\n")
            else:
                writer.write(b"APPROVED\n")
            await writer.drain()
            # Update printer info
            p['printers'] = printers
            p['width_mm'] = width_mm
            state.save()

        # Now in normal mode — register client
        async with async_lock:
            clients[client_name] = {
                "writer": writer,
                "token": token,
                "printers": printers,
                "width_mm": width_mm,
                "connected_at": datetime.now(timezone.utc).isoformat()
            }
        log.info(f"客户端上线: {client_name} | 打印机: {printers} | {width_mm}mm")

        # TCP keepalive — 防 NAT 空闲超时断连
        try:
            sock = writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except: pass

        # 心跳任务：每 15 秒发 4 字节零值保活（客户端跳过）
        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                try:
                    writer.write(b'\x00\x00\x00\x00')
                    await writer.drain()
                except: break
        hb_task = asyncio.create_task(heartbeat())

        # Read loop — 处理打印机更新
        try:
            while True:
                ln = await reader.readline()
                if not ln: break
                line = ln.decode().strip()
                # 客户端发来 JSON（打印机更新）
                if line.startswith('{'):
                    try:
                        info = json.loads(line)
                        if 'printers' in info:
                            with state.lock:
                                p = state.pairings.get(token, {})
                                p['printers'] = info['printers']
                                if 'paper_width' in info:
                                    p['width_mm'] = info['paper_width']
                                state.save()
                            log.info(f"客户端 {client_name} 打印机更新: {info['printers']}")
                    except json.JSONDecodeError:
                        pass
        except asyncio.CancelledError: pass
        finally:
            hb_task.cancel()
            async with async_lock:
                clients.pop(client_name, None)
            log.info(f"客户端断开: {client_name}")
            writer.close()
        return

    # ── Unknown command ──
    writer.write(b"UNKNOWN_CMD\n"); await writer.drain()
    writer.close()

async def send_to_client(name, data):
    async with async_lock:
        info = clients.get(name)
    if not info: return False
    w = info["writer"]
    try:
        w.write(struct.pack('>I', len(data)) + data)
        await w.drain()
        return True
    except:
        async with async_lock:
            clients.pop(name, None)
        return False

# ── HTTP Server ──────────────────────────────────────────────
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
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #eee}
th{color:#888;font-weight:500;font-size:11px}
.msg{font-size:12px;margin-top:4px}.msg.ok{color:#2e7d32}.msg.err{color:#c62828}
.empty{color:#999;font-size:13px}
</style></head><body>
<h1>🖨️ Print Relay 打印管理中心</h1>
<div class="sub">Restaurant Asia Shanghai · thecarte.eu</div>

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

<!-- ═══ 测试打印 ═══ -->
<div class="card">
<h2>🧪 测试打印</h2>
<div class="row">
  <div class="col"><label>选择路由</label><select id="test_route"><option value="">— 请先配置路由 —</option></select></div>
  <div class="col"><label>订单号</label><input id="test_order" value="TEST-001"></div>
  <div class="col"><label>菜品（名称,单价,数量|...）</label><input id="test_items" value="Peking Suppe,3.50,2|Frühlingsrolle,4.50,1"></div>
  <div class="col"><label>&nbsp;</label><label class="check"><input type="checkbox" id="test_cut"> 一菜一切</label></div>
  <div><label>&nbsp;</label><button class="btn primary" onclick="sendTest()">▶️ 发送测试</button></div>
</div>
<div id="test_msg" class="msg"></div>
</div>

<!-- ═══ 打印历史 ═══ -->
<div class="card">
<h2>📋 打印历史</h2>
<div id="history" class="empty">加载中...</div>
</div>

<script>
const P='__PANEL_TOKEN__';
let lastPd=null;

async function api(p,o={}){
 let r=await fetch(p+(p.includes('?')?'&':'?')+'panel='+P,o);
 return r.json();
}

async function refresh(){
 let d=await api('/api/pairings'); lastPd=d;
 // 配对表
 let ph='';
 if(!d.pairings||Object.keys(d.pairings).length===0){
  ph='<span class="empty">暂无配对 — 在上方输入框粘贴 Token 添加</span>';
 }else{
  ph='<table><tr><th>类型</th><th>名称</th><th>Token</th><th>打印机</th><th>操作</th></tr>';
  for(let [tok,p] of Object.entries(d.pairings)){
   let labels={woo:'WooCommerce',shopify:'Shopify',pos:'POS 系统',custom:'自定义 API',client:'客户端'};
   let icons={woo:'🛒',shopify:'🛍️',pos:'🏪',custom:'🔧',client:'💻'};
   let cls=p.type==='client'?'client':'woo';
   let icon=icons[p.type]||'📡';
   let printers=p.printers?p.printers.join(', '):(p.type==='client'?'等待客户端上线...':'—');
   ph+=`<tr><td><span class="tag ${cls}">${icon} ${labels[p.type]||p.type}</span></td><td>${p.name}</td><td><code>${tok}</code></td><td style="font-size:12px;color:#666">${printers}</td><td><button class="btn danger small" onclick="delPairing('${tok}')">删除</button></td></tr>`;
  }
  ph+='</table>';
 }
 document.getElementById('pairings').innerHTML=ph;

 // 更新路由下拉（保留选中值）
 let sources=Object.entries(d.pairings||{}).filter(([t,p])=>p.type!=='client');
 let clients=Object.entries(d.pairings||{}).filter(([t,p])=>p.type==='client');
 let ws=document.getElementById('rt_woo');
 let ws_val=ws.value;
 let cs=document.getElementById('rt_client');
 let cs_val=cs.value;
 let ps=document.getElementById('rt_printer');
 let ps_val=ps.value;

 ws.innerHTML='<option value="">— 选择来源 —</option>';
 sources.forEach(([t,p])=>ws.add(new Option(p.name+' ('+t+')',t)));
 ws.value=ws_val;  // 恢复选中（若仍存在）

 cs.innerHTML='<option value="">— 选择客户端 —</option>';
 clients.forEach(([t,p])=>cs.add(new Option(p.name,p.name)));
 cs.value=cs_val;

 // 恢复打印机下拉
 ps.innerHTML='<option value="">— 默认 —</option>';
 if(cs_val){
  let cl=clients.find(x=>x[1].name===cs_val);
  if(cl&&cl[1].printers) cl[1].printers.forEach(p=>ps.add(new Option(p,p)));
  ps.value=ps_val;
 }

 // 客户端切换时刷新打印机列表
 document.getElementById('rt_client').onchange=function(){
  let cn=this.value;
  let ps=document.getElementById('rt_printer');
  ps.innerHTML='<option value="">— 默认 —</option>';
  let cl=clients.find(x=>x[1].name===cn);
  if(cl&&cl[1].printers) cl[1].printers.forEach(p=>ps.add(new Option(p,p)));
 };

 // 路由表
 let rd=await api('/api/routes');
 let rh='',rt=document.getElementById('test_route');
  let rt_val=rt.value;
 rt.innerHTML='<option value="">— 选择路由 —</option>';
 if(!rd.routes||rd.routes.length===0){
  rh='<span class="empty">暂无路由 — 请先添加 WooCommerce 和客户端配对，然后配置路由</span>';
 }else{
  rh='<table><tr><th>订单来源</th><th>→ 客户端</th><th>→ 打印机</th><th></th></tr>';
  rd.routes.forEach((r,i)=>{
   let wn=(d.pairings&&d.pairings[r.woo_token])?d.pairings[r.woo_token].name:r.woo_token;
   rh+=`<tr><td>${wn}</td><td>${r.client||''}</td><td>${r.printer||'默认'}</td><td><button class="btn danger small" onclick="delRoute(${i})">删除</button></td></tr>`;
   rt.add(new Option(wn+' → '+r.client+' → '+(r.printer||'默认'),i));
  });
  rh+='</table>';
 }
 document.getElementById('routes').innerHTML=rh;
  rt.value=rt_val;

 // 历史
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
 let r=await fetch('/api/pairings?panel='+P,{
  method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token:tok,type:type,name:name,status:'approved'})
 });
 if(r.ok){
  document.getElementById('new_tok').value='';document.getElementById('new_name').value='';
  document.getElementById('pair_msg').className='msg ok';document.getElementById('pair_msg').textContent='✅ 已添加';
 }else{
  document.getElementById('pair_msg').className='msg err';document.getElementById('pair_msg').textContent='❌ 添加失败';
 }
 refresh();
}

async function delPairing(tok){
 if(!confirm('确认删除配对 '+tok+'？'))return;
 await api('/api/forget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
 refresh();
}

async function addRoute(){
 let woo=document.getElementById('rt_woo').value;
 let client=document.getElementById('rt_client').value;
 let printer=document.getElementById('rt_printer').value;
 if(!woo||!client){document.getElementById('route_msg').className='msg err';document.getElementById('route_msg').textContent='请选择来源和客户端';return}
 let r=await api('/api/routes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({woo_token:woo,client:client,printer:printer})});
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

async function sendTest(){
 let ri=document.getElementById('test_route').value;
 if(ri===''){document.getElementById('test_msg').className='msg err';document.getElementById('test_msg').textContent='请选择路由';return}
 let rd=await api('/api/routes');let r=rd.routes[parseInt(ri)];
 if(!r){document.getElementById('test_msg').className='msg err';document.getElementById('test_msg').textContent='路由不存在';return}
 let itemsRaw=document.getElementById('test_items').value;
 let items=[];
 itemsRaw.split('|').forEach(p=>{let x=p.split(',');if(x.length>=2)items.push({name:x[0].trim(),price:parseFloat(x[1]),quantity:parseInt(x[2]||1)})});
 let tot=items.reduce((s,i)=>s+(i.price||0)*(i.quantity||1),0).toFixed(2);
 let body=JSON.stringify({number:document.getElementById('test_order').value,date_created:new Date().toISOString(),payment_method_title:'Test',total:tot,line_items:items,shipping_total:'0.00'});
 let hdrs={'Content-Type':'application/json','X-Print-Client':r.client,'X-Printer-Name':r.printer};
 if(document.getElementById('test_cut').checked) hdrs['X-Cut-Per-Item']='1';
 let res=await fetch('/wc?token='+(r.woo_token||''),{method:'POST',headers:hdrs,body:body});
 let msg=document.getElementById('test_msg');
 try{
  let j=await res.json();
  if(res.ok){
   msg.className='msg ok';msg.textContent='✅ 已发送 → '+JSON.stringify(j);
  }else{
   msg.className='msg err';msg.textContent='❌ '+(res.status+' '+(j.detail||j.error||j.message||res.statusText));
  }
 }catch(e){
  msg.className='msg err';msg.textContent='❌ 服务器返回: '+res.status+' '+res.statusText;
 }
 refresh();
}

refresh();setInterval(refresh,10000);
</script></body></html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · Registrieren</title>
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
<div class="sub">Restaurant Asia Shanghai</div>
<p>Ihr Registrierungs-Token:</p>
<div class="code">__TOKEN__</div>
<p class="help">Kopieren Sie diesen Token und geben Sie ihn<br>im Print Relay Control Panel ein.</p>
<p class="help">Status: <span id="status">__STATUS__</span></p>
<button class="btn" onclick="location.reload()">Status prüfen</button>
</div>
<script>setTimeout(()=>location.reload(),10000);</script>
</body></html>"""

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f"HTTP {args}")

    def _panel_auth(self):
        qs = __import__('urllib').parse.parse_qs(
            __import__('urllib').parse.urlparse(self.path).query)
        return qs.get('panel', [''])[0] == state.panel_token

    def _woo_auth(self):
        qs = __import__('urllib').parse.parse_qs(
            __import__('urllib').parse.urlparse(self.path).query)
        token = qs.get('token', [''])[0]
        with state.lock:
            p = state.pairings.get(token, {})
            # 所有非客户端类型的已批准配对均可通过 HTTP POST 发单
            return p.get('type') != 'client' and p.get('status') == 'approved'

    def do_GET(self):
        path = __import__('urllib').parse.urlparse(self.path).path

        if path == '/health':
            self._json({'ok': True}); return

        if path == '/register':
            # Generate a new woo token via web
            token = secrets.token_hex(4)
            name = f"WooCommerce-{token[:4]}"
            with state.lock:
                state.pairings[token] = {"type": "woo", "name": name, "status": "pending", "created_at": datetime.now(timezone.utc).isoformat()}
                state.save()
            html = REGISTER_HTML.replace('__TOKEN__', token).replace('__STATUS__', 'Warten auf Freigabe...')
            self._html(html); return

        if path == '/api/pairings' and self._panel_auth():
            with state.lock:
                self._json({'pairings': state.pairings}); return

        if path == '/api/routes' and self._panel_auth():
            with state.lock:
                self._json({'routes': state.routes}); return

        if path == '/api/history' and self._panel_auth():
            with state.lock:
                self._json({'history': state.history}); return

        if path in ('/', '/panel'):
            html = PANEL_HTML.replace('__PANEL_TOKEN__', state.panel_token)
            self._html(html); return

        self.send_error(404)

    def do_POST(self):
        path = __import__('urllib').parse.urlparse(self.path).path
        cl = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(cl)) if cl else {}

        # WooCommerce webhook
        if path.startswith('/wc'):
            if not self._woo_auth():
                self.send_error(403, 'Unauthorized token'); return
            qs = __import__('urllib').parse.parse_qs(
                __import__('urllib').parse.urlparse(self.path).query)
            token = qs.get('token', [''])[0]

            # Find route
            target_client = self.headers.get('X-Print-Client', '')
            target_printer = self.headers.get('X-Printer-Name', '')
            cut_per_item = self.headers.get('X-Cut-Per-Item', '') == '1'

            if not target_client:
                order = body
                # 透传 cut_per_item
                if cut_per_item:
                    order = {**order, 'cut_per_item': True}
                # 收集所有匹配路由（一对多：订单 → 厨房 + 前台 + 吧台...）
                matched = []
                with state.lock:
                    for r in state.routes:
                        if r.get('woo_token') == token:
                            matched.append(r)
                if not matched:
                    log.warning(f"POST /wc: token={token} but no routes found")
                    self.send_error(503, 'No route configured'); return

                results = []
                for r in matched:
                    client = r.get('client', '')
                    printer = r.get('printer', '')

                    payload = json.dumps({"type": "print", "order": order, "printer": printer}).encode()
                    ok = run_async(send_to_client(client, payload))
                    results.append({'client': client, 'printer': printer, 'ok': ok})

                    if ok:
                        with state.lock:
                            state.history.insert(0, {
                                "time": datetime.now(timezone.utc).isoformat(),
                                "client": client, "printer": printer or "default",
                                "order": str(order.get('number', order.get('id'))),
                                "items": len(order.get('line_items', []))
                            })
                            if len(state.history) > 50: state.history.pop()

                with state.lock: state.save()
                self._json({'status': 'ok', 'results': results}); return

            else:
                # 指定目标客户端（面板测试打印）
                order = {**body}
                if cut_per_item:
                    order['cut_per_item'] = True
                payload = json.dumps({"type": "print", "order": order, "printer": target_printer}).encode()
                ok = run_async(send_to_client(target_client, payload))
                results = [{'client': target_client, 'printer': target_printer, 'ok': ok}]
                if ok:
                    with state.lock:
                        state.history.insert(0, {
                            "time": datetime.now(timezone.utc).isoformat(),
                            "client": target_client, "printer": target_printer or "default",
                            "order": str(body.get('number', 'TEST')),
                            "items": len(body.get('line_items', []))
                        })
                        if len(state.history) > 50: state.history.pop()

                with state.lock: state.save()
                self._json({'status': 'ok', 'results': results}); return

        # Panel API: approve
        if path == '/api/approve' and self._panel_auth():
            tok = body.get('token', '')
            with state.lock:
                if tok in state.pairings:
                    state.pairings[tok]['status'] = 'approved'
                    state.save()
            self._json({'ok': True}); return

        # Panel API: reject
        if path == '/api/reject' and self._panel_auth():
            tok = body.get('token', '')
            with state.lock:
                if tok in state.pairings:
                    state.pairings[tok]['status'] = 'rejected'
                    state.save()
            self._json({'ok': True}); return

        # Panel API: forget
        if path == '/api/forget' and self._panel_auth():
            tok = body.get('token', '')
            with state.lock:
                state.pairings.pop(tok, None)
                state.save()
            self._json({'ok': True}); return

        # Panel API: add route
        if path == '/api/routes' and self._panel_auth():
            with state.lock:
                state.routes.append({
                    "woo_token": body.get('woo_token', ''),
                    "client": body.get('client', ''),
                    "printer": body.get('printer', ''),
                })
                state.save()
            self._json({'ok': True}); return

        # Panel API: add pairing (白名单 — 直接 active)
        if path == '/api/pairings' and self._panel_auth():
            tok = body.get('token', '')
            if not tok or len(tok) < 4:
                self.send_error(400, 'Token too short'); return
            with state.lock:
                state.pairings[tok] = {
                    "type": body.get('type', 'woo'),
                    "name": body.get('name', tok[:6]),
                    "status": "approved",  # 白名单：添加即放行
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
                state.save()
            self._json({'ok': True, 'token': tok}); return

        # Register from external (deprecated — use panel addPairing instead)
        if path == '/api/register':
            tok = body.get('token', '')
            ptype = body.get('type', 'woo')
            name = body.get('name', ptype + '-' + tok[:4])
            with state.lock:
                if tok not in state.pairings:
                    state.pairings[tok] = {"type": ptype, "name": name, "status": "approved",
                                           "created_at": datetime.now(timezone.utc).isoformat()}
                    state.save()
            self._json({'ok': True, 'token': tok}); return

        self.send_error(404)

    def do_DELETE(self):
        path = __import__('urllib').parse.urlparse(self.path).path
        if path.startswith('/api/routes/') and self._panel_auth():
            idx = int(path.split('/')[-1])
            with state.lock:
                if 0 <= idx < len(state.routes):
                    state.routes.pop(idx)
                    state.save()
            self._json({'ok': True}); return
        self.send_error(404)

    def _json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html(self, html):
        self.send_response(200)
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
