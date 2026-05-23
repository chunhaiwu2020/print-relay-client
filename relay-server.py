#!/usr/bin/env python3
"""
Print Relay Server v3 — Multi-tenant / 多租户
Landing page with language detection + pure CN/EN panels
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

DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
ADMIN_TOKEN = "90edba8f0283b2c1"
EDITOR_HTML_FILE = DATA_DIR / "template-editor.html"
def _load_editor_html():
    try: return EDITOR_HTML_FILE.read_text()
    except: return "<h1>Editor not found</h1>"

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
        self.accounts: dict[str, dict] = {}
        self._migrated = False
        self.load()
    def load(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                if 'accounts' in d:
                    self.accounts = d['accounts']; self._migrated = True
                else:
                    self._init_admin(); a = self.accounts[ADMIN_TOKEN]
                    a['pairings'] = d.get('pairings', {}); a['routes'] = d.get('routes', []); a['history'] = d.get('history', [])[:50]
                    self._migrated = True; log.info(f"Migrated: {len(a['pairings'])} pairings")
                log.info(f"Loaded: {len(self.accounts)} accounts")
            except Exception as e:
                log.warning(f"Load error: {e}"); self._init_admin()
        else: self._init_admin(); self.save()
    def _init_admin(self):
        self.accounts[ADMIN_TOKEN] = {"email":"admin","password_hash":"","name":"Admin","status":"active","limits":{"max_clients":999},"pairings":{},"routes":[],"history":[],"created":datetime.now(timezone.utc).isoformat()}
    def save(self):
        STATE_FILE.write_text(json.dumps({'accounts':self.accounts,'version':3},ensure_ascii=False,indent=2))

state = State()

# ── Async bridge ────────────────────────────────────────────
main_loop: asyncio.AbstractEventLoop | None = None
async_lock = asyncio.Lock()
clients: dict[str, dict] = {}
def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, main_loop).result(timeout=30)

# ── TCP Server ───────────────────────────────────────────────
async def handle_client(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
    except: writer.close(); return
    parts = line.decode().strip().split(maxsplit=1)
    if not parts: writer.close(); return
    cmd = parts[0].upper()
    if cmd == 'REGISTER' and len(parts) == 2:
        token = parts[1]; found_account = None; p = None
        with state.lock:
            for atok, acct in state.accounts.items():
                if token in acct.get('pairings',{}): found_account=atok; p=acct['pairings'][token]; break
        if not p: writer.write(b"UNKNOWN_TOKEN\n"); await writer.drain(); writer.close(); return
        if p.get('type') != 'client': writer.write(b"WRONG_TYPE\n"); await writer.drain(); writer.close(); return
        client_name = p['name']
        try: jline=await asyncio.wait_for(reader.readline(),timeout=5); info=json.loads(jline.decode())
        except: writer.write(b"BAD_JSON\n"); await writer.drain(); writer.close(); return
        width_mm=info.get('paper_width',80); printers=info.get('printers',['default'])
        with state.lock:
            acct=state.accounts.get(found_account,{}); p=acct.get('pairings',{}).get(token,{})
            if p.get('status')!='approved':
                writer.write(b"PENDING\n"); await writer.drain()
                while True:
                    await asyncio.sleep(2)
                    with state.lock:
                        a=state.accounts.get(found_account,{}); pp=a.get('pairings',{}).get(token,{})
                        if pp.get('status')=='approved': break
                writer.write(b"APPROVED\n")
            else: writer.write(b"APPROVED\n")
            await writer.drain(); p['printers']=printers; p['width_mm']=width_mm; state.save()
        async with async_lock:
            clients[client_name]={"writer":writer,"token":token,"printers":printers,"width_mm":width_mm,"connected_at":datetime.now(timezone.utc).isoformat(),"account":found_account}
        log.info(f"Client online: {client_name} | {printers}")
        try:
            sock=writer.get_extra_info('socket')
            if sock: sock.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        except: pass
        async def hb():
            while True:
                await asyncio.sleep(15)
                try: writer.write(b'\x00\x00\x00\x00'); await writer.drain()
                except: break
        hbt=asyncio.create_task(hb())
        try:
            while True:
                ln=await reader.readline()
                if not ln: break
                line=ln.decode().strip()
                if line.startswith('{'):
                    try:
                        info=json.loads(line)
                        if 'printers' in info:
                            with state.lock:
                                a=state.accounts.get(found_account,{}); pp=a.get('pairings',{}).get(token,{})
                                pp['printers']=info['printers']
                                if 'paper_width' in info: pp['width_mm']=info['paper_width']
                                state.save()
                    except json.JSONDecodeError: pass
        except asyncio.CancelledError: pass
        finally:
            hbt.cancel()
            async with async_lock: clients.pop(client_name,None)
            log.info(f"Client offline: {client_name}"); writer.close()
        return
    writer.write(b"UNKNOWN_CMD\n"); await writer.drain(); writer.close()

async def send_to_client(name, data):
    async with async_lock: info=clients.get(name)
    if not info: return False
    try: info["writer"].write(struct.pack('>I',len(data))+data); await info["writer"].drain(); return True
    except Exception:
        async with async_lock: clients.pop(name,None)
        return False

# ── ESC/POS 云端渲染引擎 ─────────────────────────────────────
import re as _re, base64 as _b64

VAR_MAP = {
    'restaurant_name': 'Restaurant Asia Shanghai',
    'id': lambda o: str(o.get('number', o.get('id', '?'))),
    'table_id': lambda o: str(o.get('table_id', o.get('table', '?'))),
    'table': lambda o: str(o.get('table', o.get('table_id', '?'))),
    'printed_at': lambda o: datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
    'total_amount': lambda o: str(o.get('total', '0')),
    'qty': lambda item: str(item.get('quantity', 1)),
    'item_code': lambda item: str(item.get('sku', item.get('id', ''))),
    'name': lambda item: str(item.get('name', '')),
    'price': lambda item: str(item.get('price', '0')),
    'total': lambda item: str(item.get('total', item.get('price', '0'))),
}

def _resolve_vars(tmpl, order, item=None):
    def repl(m):
        key = m.group(1)
        if key in VAR_MAP:
            v = VAR_MAP[key]
            return v(item) if callable(v) and item is not None else v(order) if callable(v) else str(v)
        return m.group(0)
    return _re.sub(r'\{\{(\w+)\}\}', repl, tmpl)

def _esc_init(): return b'\x1b\x40'
def _esc_align(a='left'): return b'\x1b\x61' + bytes([{'left':0,'center':1,'right':2}.get(a,0)])
def _esc_bold(on=True): return b'\x1b\x45' + bytes([1 if on else 0])
def _esc_size(on, s=''):
    if not on or not s: return b''
    return b'\x1d\x21\x11' if s == 'xl' else b'\x1d\x21\x10' if s == 'wide' else b''
def _esc_size_off(): return b'\x1d\x21\x00'
def _esc_line(text, w_mm):
    import unicodedata
    text = text.replace('€', 'EUR')
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    n = int(w_mm * 1.8)
    return text.encode('latin-1', errors='replace')[:n] + b'\n'

def render_ticket(template, order, station_filter=None):
    """JSON模板 → ESC/POS bytes"""
    w = template.get('width', 48)
    out = _esc_init()
    items = order.get('line_items', [])
    if station_filter:
        items = [i for i in items if station_filter.lower() in i.get('name','').lower()]

    for el in template.get('lines', []):
        if 'hr' in el:
            ch = el.get('hr', '-'); n = int(w * 1.8)
            out += (ch * n).encode()[:n] + b'\n'
            continue
        if el.get('repeat') == 'items':
            for item in items:
                sz = el.get('size', ''); bold = el.get('bold', False)
                if sz: out += _esc_size(True, sz)
                if bold: out += _esc_bold(True)
                if 'text' in el:
                    txt = _resolve_vars(el['text'], order, item)
                    al = el.get('align', 'left')
                    if al != 'left': out += _esc_align(al)
                    out += _esc_line(txt, w)
                elif 'left' in el or 'right' in el:
                    l = _resolve_vars(el.get('left',''), order, item)
                    r = _resolve_vars(el.get('right',''), order, item)
                    n = int(w * 1.8)
                    out += _esc_line(l.ljust(n - len(r)) + r, w)
                if sz: out += _esc_size_off()
                if bold: out += _esc_bold(False)
            continue
        if 'text' in el:
            txt = _resolve_vars(el['text'], order)
            al = el.get('align', 'left'); bold = el.get('bold', False); sz = el.get('size', '')
            if sz: out += _esc_size(True, sz)
            if bold: out += _esc_bold(True)
            if al != 'left': out += _esc_align(al)
            out += _esc_line(txt, w)
            if sz: out += _esc_size_off()
            if bold: out += _esc_bold(False)
    # Cut: push paper 15mm → partial cut → retract
    out += b'\x1b\x4a\x78'     # ESC J 120 (15mm feed)
    out += b'\x1d\x56\x01'     # GS V 1 (partial cut)
    out += b'\x1b\x4b\x78'     # ESC K 120 (15mm reverse feed)
    return out

def load_and_render(route, order):
    """加载路由指定的模板，渲染成 base64 ticket"""
    tpl_path = route.get('template', '')
    override = route.get('template_override', '')
    if override:
        template = json.loads(override)
    elif tpl_path:
        fp = DATA_DIR / 'templates' / tpl_path
        if fp.is_file():
            template = json.loads(fp.read_text())
        else:
            return None
    else:
        return None
    sf = route.get('station_filter', None)
    ticket = render_ticket(template, order, sf)
    return _b64.b64encode(ticket).decode()

# ── PDF Rendering ──────────────────────────────────────────
def _render_html_content(template, order):
    """Render HTML template content rows into full HTML string"""
    rows_html = []
    for row in template.get('content', []):
        # Support extended row format: [tag, class, text] or ["condition", key, tag, class, text]
        if row[0] == 'condition':
            if len(row) < 5: continue
            cond_key, tag, cls, text = row[1], row[2], row[3], row[4]
            if cond_key not in order or not order.get(cond_key):
                continue
        else:
            tag, cls, text = row[0], row[1], row[2] if len(row) > 2 else ''
        
        text = _resolve_vars(text, order)
        if tag == 'div':
            if cls == 'hr':
                rows_html.append('<div class="hr"></div>')
            elif cls == 'row':
                rows_html.append(f'<div class="row">{text}</div>')
            else:
                rows_html.append(f'<div class="{cls}">{text}</div>')
    
    tpl = template.get('html', '<html><body>%CONTENT%</body></html>')
    return tpl.replace('%CONTENT%', '\n'.join(rows_html))

def render_pdf(template, order):
    """Convert HTML template + order data → PDF bytes"""
    from weasyprint import HTML
    html = _render_html_content(template, order)
    return HTML(string=html).write_pdf(presentational_hints=True)

def load_and_render_pdf(route, order):
    """Load HTML template, render PDF, return base64"""
    tpl_path = route.get('template', '')
    fp = DATA_DIR / 'templates' / tpl_path
    if not fp.is_file():
        return None
    template = json.loads(fp.read_text())
    if template.get('format') != 'html':
        return None
    pdf = render_pdf(template, order)
    return _b64.b64encode(pdf).decode()

def load_and_render_any(route, order):
    """Auto-detect format: PDF for 'html' templates, ESC/POS otherwise"""
    tpl_path = route.get('template', '')
    fp = DATA_DIR / 'templates' / tpl_path
    if not fp.is_file():
        return None, None
    template = json.loads(fp.read_text())
    if template.get('format') == 'html':
        pdf = render_pdf(template, order)
        return _b64.b64encode(pdf).decode(), 'pdf'
    else:
        sf = route.get('station_filter', None)
        ticket = render_ticket(template, order, sf)
        return _b64.b64encode(ticket).decode(), 'escpos'

# ── i18n strings ─────────────────────────────────────────────
T = {
    "zh": {
        "title": "Print Relay · 云端打印中继",
        "hero": "订单直达打印机，零配置云端中继",
        "sub": "餐厅后厨、收银台、吧台 — 多站自动分发，逐菜切纸，开机即用",
        "features": ["🔌 一键配对","🖨️ 多站分发","📋 多租户隔离","🔒 邮箱注册","🚀 开机自启","🌍 中英双语"],
        "cta": "开始使用",
        "login_btn": "登录",
        "footer": "© 2026 Print Relay · 简约高效的云端打印方案",
        "login_title": "登录",
        "register_title": "注册",
        "email_ph": "邮箱地址",
        "password_ph": "密码",
        "password_hint": "最少6位",
        "login_action": "登录",
        "register_action": "注册",
        "reg_success": "注册成功！等待管理员审核",
        "panel_title": "面板",
        "pairings_title": "已配对设备",
        "routes_title": "打印路由",
        "test_title": "测试打印",
        "history_title": "打印历史",
        "loading": "加载中...",
        "no_pairings": "暂无配对 — 在上方粘贴 Token 添加",
        "type_col": "类型",
        "name_col": "名称",
        "status_col": "状态",
        "printer_col": "打印机",
        "action_col": "操作",
        "delete_btn": "删除",
        "pending": "⏳待审",
        "approved": "✅已批",
        "rejected": "❌拒绝",
        "offline": "等待上线",
        "add_btn": "➕ 添加",
        "source_label": "订单来源",
        "client_label": "目标客户端",
        "printer_label": "打印机",
        "default_printer": "— 默认 —",
        "select_source": "— 选择来源 —",
        "select_client": "— 选择客户端 —",
        "add_route": "+ 添加",
        "route_added": "✅ 路由已添加",
        "route_failed": "❌ 失败",
        "no_routes": "暂无路由 — 请先添加配对和客户端",
        "test_route_label": "选择路由",
        "test_order_label": "订单号",
        "test_items_label": "菜品 (名称,单价,数量|...)",
        "send_test": "▶️ 发送",
        "sent": "✅ 已发送",
        "select_route_first": "请先配置路由",
        "history_time": "时间",
        "history_client": "客户端",
        "history_printer": "打印机",
        "history_order": "订单",
        "no_history": "暂无记录",
        "token_short": "Token 太短",
        "limit_reached": "已达客户端上限",
        "added": "✅ 已添加",
        "confirm_delete": "确认删除配对？",
        "select_both": "请选择来源和客户端",
        "route_not_found": "路由不存在",
        "server_error": "服务器错误",
        "clients_label": "客户端",
        "logout": "退出",
        "admin_title": "管理员",
        "admin_pending": "待审核",
        "admin_all": "全部账号",
        "admin_email": "邮箱",
        "admin_name": "名称",
        "admin_registered": "注册时间",
        "admin_action": "操作",
        "admin_status": "状态",
        "admin_status_pending": "待审",
        "admin_status_active": "活跃",
        "admin_status_suspended": "暂停",
        "approve": "批准",
        "reject": "拒绝",
        "suspend": "暂停",
        "resume": "恢复",
        "limit_label": "上限",
        "save": "保存",
        "updated": "✅ 已更新",
        "no_pending": "无待审核",
    },
    "en": {
        "title": "Print Relay · Cloud Print Relay",
        "hero": "Orders to Printer, Zero-Config Cloud Relay",
        "sub": "Kitchen, Cashier, Bar — Multi-station auto-routing, per-item cutting, plug & play",
        "features": ["🔌 One-Click Pairing","🖨️ Multi-Station","📋 Multi-Tenant","🔒 Email Signup","🚀 Auto-Start","🌍 CN/EN"],
        "cta": "Get Started",
        "login_btn": "Login",
        "footer": "© 2026 Print Relay · Simple & Efficient Cloud Printing",
        "login_title": "Login",
        "register_title": "Register",
        "email_ph": "Email address",
        "password_ph": "Password",
        "password_hint": "Min 6 characters",
        "login_action": "Login",
        "register_action": "Register",
        "reg_success": "Registered! Awaiting admin approval.",
        "panel_title": "Panel",
        "pairings_title": "Paired Devices",
        "routes_title": "Print Routes",
        "test_title": "Test Print",
        "history_title": "Print History",
        "loading": "Loading...",
        "no_pairings": "No pairings — Paste a token above to add",
        "type_col": "Type",
        "name_col": "Name",
        "status_col": "Status",
        "printer_col": "Printers",
        "action_col": "Action",
        "delete_btn": "Del",
        "pending": "⏳Pending",
        "approved": "✅Approved",
        "rejected": "❌Rejected",
        "offline": "Offline",
        "add_btn": "➕ Add",
        "source_label": "Source",
        "client_label": "Client",
        "printer_label": "Printer",
        "default_printer": "— Default —",
        "select_source": "— Select Source —",
        "select_client": "— Select Client —",
        "add_route": "+ Add Route",
        "route_added": "✅ Route added",
        "route_failed": "❌ Failed",
        "no_routes": "No routes — Add pairings first",
        "test_route_label": "Select Route",
        "test_order_label": "Order #",
        "test_items_label": "Items (name,price,qty|...)",
        "send_test": "▶️ Send",
        "sent": "✅ Sent",
        "select_route_first": "Configure routes first",
        "history_time": "Time",
        "history_client": "Client",
        "history_printer": "Printer",
        "history_order": "Order",
        "no_history": "No records",
        "token_short": "Token too short",
        "limit_reached": "Client limit reached",
        "added": "✅ Added",
        "confirm_delete": "Confirm delete pairing?",
        "select_both": "Select source and client",
        "route_not_found": "Route not found",
        "server_error": "Server error",
        "clients_label": "Clients",
        "logout": "Logout",
        "admin_title": "Admin",
        "admin_pending": "Pending Approval",
        "admin_all": "All Accounts",
        "admin_email": "Email",
        "admin_name": "Name",
        "admin_registered": "Registered",
        "admin_action": "Action",
        "admin_status": "Status",
        "admin_status_pending": "Pending",
        "admin_status_active": "Active",
        "admin_status_suspended": "Suspended",
        "approve": "Approve",
        "reject": "Reject",
        "suspend": "Suspend",
        "resume": "Resume",
        "limit_label": "Limit",
        "save": "Save",
        "updated": "✅ Updated",
        "no_pending": "No pending",
        "templates_title": "📋 Templates",
        "templates_pick": "Template", "templates_preview": "Preview", "templates_apply": "Apply",
        "templates_desc": "Browse and apply print templates for your industry",
        "templates_all": "All",
        "edit_tpl": "✏️ Customize", "save_tpl": "💾 Save Custom", "clear_tpl": "Clear Custom", "cancel": "Cancel",
        "pick_tpl_first": "Select a template first", "tpl_customized": "Customized",
    },
    "zh": {
        "login_title": "登录", "register_title": "注册", "email_ph": "邮箱地址", "password_ph": "密码", "password_hint": "至少6位",
        "login_action": "登录", "register_action": "注册", "reg_success": "注册成功，等待管理员审核",
        "panel_title": "控制面板", "pairings_title": "已配对设备", "routes_title": "路由配置", "test_title": "测试打印",
        "history_title": "打印历史", "loading": "加载中...", "no_pairings": "暂无配对", "type_col": "类型", "name_col": "名称",
        "status_col": "状态", "printer_col": "打印机", "action_col": "操作", "delete_btn": "删除", "pending": "⏳待审",
        "approved": "✅已批准", "rejected": "❌拒绝", "offline": "离线", "add_btn": "➕ 添加", "source_label": "来源",
        "client_label": "客户端", "printer_label": "打印机", "default_printer": "— 默认 —", "select_source": "— 选来源 —",
        "select_client": "— 选客户端 —", "add_route": "+ 添加路由", "route_added": "✅ 已添加", "route_failed": "❌ 失败",
        "no_routes": "暂无路由", "test_route_label": "选择路由", "test_order_label": "订单号",
        "test_items_label": "菜品 (名称,价格,数量|...)", "send_test": "▶️ 发送", "sent": "✅ 已发送",
        "select_route_first": "请先配置路由", "history_time": "时间", "history_client": "客户端",
        "history_printer": "打印机", "history_order": "订单", "no_history": "暂无记录", "token_short": "Token 太短",
        "limit_reached": "已达客户端上限", "added": "✅ 已添加", "confirm_delete": "确认删除配对？",
        "select_both": "请选择来源和客户端", "route_not_found": "路由不存在", "server_error": "服务器错误",
        "clients_label": "客户端", "logout": "退出", "admin_title": "管理员", "admin_pending": "待审核",
        "admin_all": "全部账号", "admin_email": "邮箱", "admin_name": "名称", "admin_registered": "注册时间",
        "admin_action": "操作", "admin_status": "状态", "admin_status_pending": "待审", "admin_status_active": "活跃",
        "admin_status_suspended": "暂停", "approve": "批准", "reject": "拒绝", "suspend": "暂停", "resume": "恢复",
        "limit_label": "上限", "save": "保存", "updated": "✅ 已更新", "no_pending": "无待审核",
        "title": "Print Relay · 云打印中继", "hero": "订单直达打印机，零配置云打印中继",
        "sub": "厨房·收银·吧台多站分发，逐菜切纸自适配，插电即用",
        "features": ["🔌 一键配对","🖨️ 多站分发","📋 多租户","🔒 邮箱注册","🚀 开机自启","🌍 中英双语"],
        "cta": "立即开始", "login_btn": "登录", "footer": "© 2026 Print Relay · 简单高效的云打印",
        "templates_title": "📋 模板市场", "templates_pick": "模板", "templates_preview": "预览",
        "templates_apply": "应用", "templates_desc": "选择适合您行业的打印模板", "templates_all": "全部",
        "edit_tpl": "✏️ 自定义", "save_tpl": "💾 保存自定义", "clear_tpl": "清除自定义", "cancel": "取消",
        "pick_tpl_first": "请先选择模板", "tpl_customized": "已自定义",
    }
}

def _page_css():
    return """*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:#f0f2f5;color:#222}
.header{background:#fff;border-bottom:1px solid #eee;padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:56px}
.header .logo{font-size:18px;font-weight:700}.header .logo span{color:#1976d2}
.header .lang a{color:#888;text-decoration:none;font-size:13px;margin-left:12px}
.header .lang a.active{color:#1976d2;font-weight:600}
.hero{text-align:center;padding:80px 24px 60px;max-width:700px;margin:0 auto}
.hero h1{font-size:32px;margin-bottom:12px}.hero h1 span{color:#1976d2}
.hero p{color:#666;font-size:16px;margin-bottom:32px}
.hero .cta a{display:inline-block;padding:14px 40px;background:#1976d2;color:#fff;border-radius:8px;text-decoration:none;font-size:16px;font-weight:600;margin:0 8px}
.hero .cta a.ghost{background:#fff;color:#1976d2;border:2px solid #1976d2}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;max-width:800px;margin:0 auto 60px;padding:0 24px}
.features .feat{background:#fff;border-radius:10px;padding:24px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.04);font-size:14px;font-weight:500}
.footer{text-align:center;color:#999;font-size:12px;padding:32px 0}

/* Panel pages */
.container{max-width:1100px;margin:0 auto;padding:20px}
.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card h2{font-size:15px;margin-bottom:10px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}.col{flex:1;min-width:180px}
.col label{display:block;font-size:12px;color:#666;margin-bottom:3px}
input,select{width:100%;padding:7px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px}
input:focus,select:focus{outline:none;border-color:#1976d2}
.btn{padding:7px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;white-space:nowrap}
.btn.primary{background:#1976d2;color:#fff}.btn.primary:hover{background:#1565c0}
.btn.success{background:#2e7d32;color:#fff}.btn.success:hover{background:#1b5e20}
.btn.danger{background:#c62828;color:#fff}.btn.danger:hover{background:#b71c1c}
.btn.small{padding:4px 10px;font-size:11px}
.btn.approve{background:#2e7d32;color:#fff}.btn.reject{background:#e65100;color:#fff}.btn.suspend{background:#c62828;color:#fff}.btn.activate{background:#1976d2;color:#fff}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.tag.woo{background:#e3f2fd;color:#1565c0}.tag.client{background:#e8f5e9;color:#2e7d32}
.tag.pending{background:#fff3e0;color:#e65100}.tag.active{background:#e8f5e9;color:#2e7d32}.tag.suspended{background:#ffebee;color:#c62828}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #eee}
th{color:#888;font-weight:500;font-size:11px}
.msg{font-size:12px;margin-top:4px}.msg.ok{color:#2e7d32}.msg.err{color:#c62828}
.empty{color:#999;font-size:13px}
.sub{color:#888;font-size:12px;margin-bottom:18px}
.limits input{width:40px;padding:2px 4px;border:1px solid #ccc;border-radius:4px;font-size:12px;text-align:center}
.login-card{background:#fff;border-radius:12px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:400px;width:100%;margin:80px auto}
.login-card h1{font-size:22px;text-align:center;margin-bottom:4px}
.login-card .sub{text-align:center;color:#888;font-size:13px;margin-bottom:24px}
.login-card label{display:block;font-size:12px;color:#666;margin-bottom:4px}
.login-card input{width:100%;padding:10px 12px;border:1px solid #ccc;border-radius:8px;font-size:14px;margin-bottom:12px}
.login-card input:focus{outline:none;border-color:#1976d2}
.login-card .btn{width:100%;padding:11px;background:#1976d2;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-weight:500;margin-top:4px}
.login-card .btn:hover{background:#1565c0}
.tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #eee}
.tab{flex:1;text-align:center;padding:10px;cursor:pointer;font-weight:500;color:#888;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab.active{color:#1976d2;border-bottom-color:#1976d2}
.tmpl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
.tmpl-card{background:#f8fafc;border:1px solid #e8ecf0;border-radius:8px;padding:12px;cursor:pointer;transition:all .15s}
.tmpl-card:hover{border-color:#1976d2;box-shadow:0 2px 8px rgba(25,118,210,.1)}
.tmpl-card .tname{font-weight:600;font-size:13px;margin-bottom:3px}
.tmpl-card .tdesc{font-size:11px;color:#888}
.tmpl-card .tind{display:inline-block;margin-top:6px;padding:1px 6px;border-radius:3px;font-size:10px;background:#e3f2fd;color:#1565c0}
.tmpl-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tmpl-tab{padding:4px 12px;border-radius:14px;font-size:12px;cursor:pointer;border:1px solid #ddd;background:#fff;white-space:nowrap}
.tmpl-tab.active{background:#1976d2;color:#fff;border-color:#1976d2}
.tmpl-preview{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);z-index:999;align-items:center;justify-content:center}
.tmpl-preview.show{display:flex}
.tmpl-preview .box{background:#fff;border-radius:12px;padding:24px;max-width:500px;width:90%;max-height:80vh;overflow-y:auto}
.tmpl-preview .box h3{font-size:16px;margin-bottom:12px}
.tmpl-preview .box pre{font-size:11px;background:#f5f5f5;padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;max-height:45vh}
"""

def _lang_nav(lang):
    zh_cls = "active" if lang == "zh" else ""
    en_cls = "active" if lang == "en" else ""
    return f'<div class="lang"><a href="?lang=zh" class="{zh_cls}" onclick="setLang(\'zh\')">中文</a><a href="?lang=en" class="{en_cls}" onclick="setLang(\'en\')">EN</a></div>'

def _lang_script():
    return """<script>
function setLang(l){localStorage.setItem('pr_lang',l);location.search='?lang='+l}
(function(){
 let l=new URLSearchParams(location.search).get('lang')||localStorage.getItem('pr_lang');
 if(!l){l=(navigator.language||'').startsWith('zh')?'zh':'en';localStorage.setItem('pr_lang',l)}
 if(!location.search.includes('lang=')){let s=location.search;location.search=(s?s+'&':'')+'lang='+l}
})()</script>"""

def landing_page(lang):
    t = T[lang]; css = _page_css()
    return f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t['title']}</title><style>{css}</style></head><body>
<div class="header"><div class="logo">🖨️ <span>Print Relay</span></div>{_lang_nav(lang)}</div>
<div class="hero">
<h1>{t['hero']}</h1>
<p>{t['sub']}</p>
<div class="cta"><a href="/login?lang={lang}">{t['cta']}</a><a href="/login?lang={lang}" class="ghost">{t['login_btn']}</a></div>
</div>
<div class="features">{''.join(f'<div class="feat">{f}</div>' for f in t['features'])}</div>
<div class="footer">{t['footer']}</div>
{_lang_script()}
</body></html>"""

def login_page(lang):
    t = T[lang]; css = _page_css()
    return f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · {t['login_title']}</title><style>{css}</style></head><body>
<div class="header"><div class="logo">🖨️ <span>Print Relay</span></div>{_lang_nav(lang)}</div>
<div class="login-card">
<h1>🖨️ Print Relay</h1>
<div class="sub">{t['sub']}</div>
<div class="tabs"><div class="tab active" onclick="showTab('login')">{t['login_title']}</div><div class="tab" onclick="showTab('register')">{t['register_title']}</div></div>
<div id="form-login">
<label>{t['email_ph']}</label><input id="l_email" type="email">
<label>{t['password_ph']}</label><input id="l_password" type="password">
<button class="btn" onclick="doLogin()">{t['login_action']}</button>
</div>
<div id="form-register" style="display:none">
<label>{t['email_ph']}</label><input id="r_email" type="email">
<label>{t['password_ph']}</label><input id="r_password" type="password" placeholder="{t['password_hint']}">
<button class="btn" onclick="doRegister()">{t['register_action']}</button>
</div>
<div id="msg" class="msg"></div>
</div>
{_lang_script()}
<script>
function showTab(t){{
 document.querySelectorAll('.tab').forEach((e,i)=>e.classList.toggle('active',i==(t==='login'?0:1)));
 document.getElementById('form-login').style.display=t==='login'?'block':'none';
 document.getElementById('form-register').style.display=t==='register'?'block':'none';
 document.getElementById('msg').textContent='';
}}
async function doLogin(){{
 let e=document.getElementById('l_email').value.trim(),p=document.getElementById('l_password').value;
 if(!e||!p){{msg('{t["token_short"]}','err');return}}
 let r=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:e,password:p}})}});
 let d=await r.json();
 if(r.ok){{msg('OK, redirecting...','ok');setTimeout(()=>location.href='/panel?token='+d.token+'&lang='+(new URLSearchParams(location.search).get('lang')||'en'),500)}}
 else msg(d.error||'Failed','err');
}}
async function doRegister(){{
 let e=document.getElementById('r_email').value.trim(),p=document.getElementById('r_password').value;
 if(!e||p.length<6){{msg('{t["password_hint"]}','err');return}}
 let r=await fetch('/api/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:e,password:p}})}});
 let d=await r.json();
 if(r.ok) msg('{t["reg_success"]}','ok');
 else msg(d.error||'Failed','err');
}}
function msg(t,c){{let m=document.getElementById('msg');m.textContent=t;m.className='msg '+c}}
</script></body></html>"""

def panel_page(lang, token):
    t = T[lang]; css = _page_css()
    return f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · {t['panel_title']}</title><style>{css}</style></head><body>
<div class="header"><div class="logo">🖨️ <span>Print Relay</span></div>{_lang_nav(lang)}</div>
<div class="container">
<h1>🖨️ Print Relay <span style="font-size:14px;color:#888" id="acct_name"></span></h1>
<div class="sub"><span id="acct_email"></span> · {t['clients_label']} <span id="acct_clients">0/0</span> · <a href="https://api.printrelay.es/editor?v=2" target="_blank" style="color:#e94560;text-decoration:none;font-weight:500">🎨 模板编辑器</a></div>

<div class="card"><h2>📡 {t['pairings_title']}</h2>
<div id="pairings" class="empty">{t['loading']}</div>
<div class="row" style="margin-top:12px;align-items:stretch">
<div class="col"><input id="new_tok" placeholder="Paste Token"></div>
<div class="col" style="min-width:100px"><select id="new_type"><option value="woo">🛒 WooCommerce</option><option value="shopify">🛍️ Shopify</option><option value="pos">🏪 POS</option><option value="custom">🔧 Custom API</option><option value="client">💻 Client</option></select></div>
<div class="col"><input id="new_name" placeholder="Name"></div>
<div><button class="btn primary" onclick="addPairing()" style="height:37px">{t['add_btn']}</button></div>
</div><div id="pair_msg" class="msg"></div></div>

<div class="card"><h2>🔀 {t['routes_title']}</h2>
<div id="routes" class="empty">{t['loading']}</div>
<div class="row" style="margin-top:12px">
<div class="col"><label>{t['source_label']}</label><select id="rt_woo"><option value="">{t['select_source']}</option></select></div>
<div class="col"><label>{t['client_label']}</label><select id="rt_client"><option value="">{t['select_client']}</option></select></div>
<div class="col"><label>{t['printer_label']}</label><select id="rt_printer"><option value="">{t['default_printer']}</option></select></div>
<div class="col"><label>{t['templates_pick']}</label><div style="display:flex;gap:3px"><select id="rt_template" style="flex:1" onchange="clearRouteTpl()"><option value="">— {t['default_printer']} —</option></select><button class="btn small" onclick="editTplForRoute()" title="{t['edit_tpl']}" id="btn_edit_tpl" style="padding:4px 8px">✏️</button></div></div>
<div><label>&nbsp;</label><button class="btn success" onclick="addRoute()">{t['add_route']}</button></div>
</div><div id="route_msg" class="msg"></div></div>

<div class="card"><h2>{t['templates_title']}</h2><p class="sub">{t['templates_desc']}</p>
<div class="tmpl-tabs" id="tmpl_tabs"></div>
<div class="tmpl-grid" id="tmpl_grid"><span class="empty">{t['loading']}</span></div>
<div class="tmpl-preview" id="tmpl_preview" onclick="if(event.target===this)closePreview()">
<div class="box"><h3 id="tmpl_pname"></h3><pre id="tmpl_pbody"></pre>
<button class="btn primary" style="margin-top:10px" onclick="closePreview()">✕ Close</button></div>
</div></div>

<div class="tmpl-preview" id="tpl_editor" onclick="if(event.target===this)closeTplEditor()">
<div class="box" style="max-width:650px">
<h3>✏️ <span id="tpl_edit_name" style="color:#888;font-weight:400"></span></h3>
<textarea id="tpl_edit_body" style="width:100%;height:350px;font:11px/1.4 monospace;border:1px solid #ccc;border-radius:6px;padding:10px;resize:vertical;tab-size:2" spellcheck="false"></textarea>
<div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
<button class="btn danger small" onclick="clearRouteTpl()" style="margin-right:auto">清除自定义</button>
<button class="btn" onclick="closeTplEditor()">取消</button>
<button class="btn primary" onclick="saveRouteTpl()">💾 保存自定义</button>
</div>
</div></div>

<div class="card"><h2>🧪 {t['test_title']}</h2>
<div class="row">
<div class="col"><label>{t['test_route_label']}</label><select id="test_route" onchange="onTestRouteChange()"><option value="">— {t['select_route_first']} —</option></select></div>
<div class="col"><label>{t['templates_pick']}</label><div style="display:flex;gap:3px"><select id="test_template" style="flex:1"><option value="">— {t['default_printer']} —</option></select><button class="btn small" onclick="editTestTpl()" title="{t['edit_tpl']}" id="btn_test_edit_tpl" style="padding:4px 8px">✏️</button></div></div>
<div class="col"><label>{t['test_order_label']}</label><input id="test_order" value="TEST-001"></div>
<div class="col"><label>{t['test_items_label']}</label><input id="test_items" value="Peking Suppe,3.50,2|Frühlingsrolle,4.50,1"></div>
<div><label>&nbsp;</label><button class="btn primary" onclick="sendTest()">{t['send_test']}</button></div>
</div><div id="test_msg" class="msg"></div></div>

<div class="card"><h2>📋 {t['history_title']}</h2><div id="history" class="empty">{t['loading']}</div></div>
</div>
{_lang_script()}
<script>
const T='{token}';
async function api(p,o={{}}){{let s=p.includes('?')?'&':'?';let r=await fetch(p+s+'token='+T,o);return r.json()}}
async function refresh(){{
 let d=await api('/api/pairings');
 document.getElementById('acct_name').textContent=d.account_name||'';
 document.getElementById('acct_email').textContent=d.account_email||'';
 document.getElementById('acct_clients').textContent=(d.client_count||0)+'/'+(d.max_clients||3);
 let pairings=d.pairings||{{}};
 let ph='';
 if(Object.keys(pairings).length===0){{
  ph='<span class="empty">{t["no_pairings"]}</span>';
 }}else{{
  ph='<table><tr><th>{t["type_col"]}</th><th>{t["name_col"]}</th><th>Token</th><th>{t["status_col"]}</th><th>{t["printer_col"]}</th><th>{t["action_col"]}</th></tr>';
  for(let [tok,p] of Object.entries(pairings)){{
   let labels={{woo:'WooCommerce',shopify:'Shopify',pos:'POS',custom:'Custom',client:'Client'}};
   let icons={{woo:'🛒',shopify:'🛍️',pos:'🏪',custom:'🔧',client:'💻'}};
   let st={{pending:'{t["pending"]}',approved:'{t["approved"]}',rejected:'{t["rejected"]}'}}[p.status]||p.status;
   let pr=p.printers?p.printers.join(', '):(p.type==='client'?'{t["offline"]}':'—');
   ph+=`<tr><td><span class="tag ${{p.type==='client'?'client':'woo'}}">${{icons[p.type]||'📡'}} ${{labels[p.type]||p.type}}</span></td><td>${{p.name}}</td><td><code>${{tok}}</code></td><td>${{st}}</td><td style="font-size:12px;color:#666">${{pr}}</td><td><button class="btn danger small" onclick="delPairing('${{tok}}')">{t["delete_btn"]}</button></td></tr>`;
  }}
  ph+='</table>';
 }}
 document.getElementById('pairings').innerHTML=ph;
 let sources=Object.entries(pairings).filter(([t,p])=>p.type!=='client');
 let cls=Object.entries(pairings).filter(([t,p])=>p.type==='client');
 let ws=document.getElementById('rt_woo'),wsv=ws.value;
 let cs=document.getElementById('rt_client'),csv=cs.value;
 let ps=document.getElementById('rt_printer'),psv=ps.value;
 ws.innerHTML='<option value="">{t["select_source"]}</option>';sources.forEach(([t,p])=>ws.add(new Option(p.name+' ('+t+')',t)));ws.value=wsv;
 cs.innerHTML='<option value="">{t["select_client"]}</option>';cls.forEach(([t,p])=>cs.add(new Option(p.name,p.name)));cs.value=csv;
 ps.innerHTML='<option value="">{t["default_printer"]}</option>';
 if(csv){{let cl=cls.find(x=>x[1].name===csv);if(cl&&cl[1].printers)cl[1].printers.forEach(p=>ps.add(new Option(p,p)));ps.value=psv}}
 document.getElementById('rt_client').onchange=function(){{let ps2=document.getElementById('rt_printer');ps2.innerHTML='<option value="">{t["default_printer"]}</option>';let c2=cls.find(x=>x[1].name===this.value);if(c2&&c2[1].printers)c2[1].printers.forEach(p=>ps2.add(new Option(p,p)))}}
 let rd=await api('/api/routes');
 let rh='';
 if(!rd.routes||rd.routes.length===0){{rh='<span class="empty">{t["no_routes"]}</span>'}}
 else{{
  rh='<table><tr><th>{t["source_label"]}</th><th>→ {t["client_label"]}</th><th>→ {t["printer_label"]}</th><th>{t["templates_pick"]}</th><th></th></tr>';
  rd.routes.forEach((r,i)=>{{let wn=(pairings[r.woo_token])?pairings[r.woo_token].name:r.woo_token;let tplCell=r.template||'—';if(r.template_override)tplCell+=' <span style="background:#fff3e0;color:#e65100;padding:1px 5px;border-radius:3px;font-size:10px">✏️</span>';rh+=`<tr><td>${{wn}}</td><td>${{r.client||''}}</td><td>${{r.printer||'{t["default_printer"]}'}}</td><td style="font-size:11px;color:#888">${{tplCell}}</td><td><button class="btn danger small" onclick="delRoute(${{i}})">{t["delete_btn"]}</button></td></tr>`}});
  rh+='</table>';
 }}
 document.getElementById('routes').innerHTML=rh;
 let tr=document.getElementById('test_route'),trv=tr.value;
 tr.innerHTML='<option value="">— {t["select_route_first"]} —</option>';
 if(rd.routes&&rd.routes.length>0){{rd.routes.forEach((r,i)=>{{let wn=(pairings[r.woo_token])?pairings[r.woo_token].name:r.woo_token;tr.add(new Option(wn+' → '+r.client+' → '+(r.printer||'{t["default_printer"]}'),i))}});tr.value=trv}}
 let hd=await api('/api/history');
 let hh='';
 if(!hd.history||hd.history.length===0)hh='<span class="empty">{t["no_history"]}</span>';
 else{{
  hh='<table><tr><th>{t["history_time"]}</th><th>{t["history_client"]}</th><th>{t["history_printer"]}</th><th>{t["history_order"]}</th></tr>';
  hd.history.slice(0,20).forEach(e=>hh+=`<tr><td>${{(e.time||'').slice(11,19)}}</td><td>${{e.client}}</td><td>${{e.printer}}</td><td>#${{e.order}} (${{e.items}} items)</td></tr>`);
  hh+='</table>';
 }}
 document.getElementById('history').innerHTML=hh;
}}
async function addPairing(){{
 let tok=document.getElementById('new_tok').value.trim();
 if(!tok||tok.length<4){{document.getElementById('pair_msg').className='msg err';document.getElementById('pair_msg').textContent='{t["token_short"]}';return}}
 let type=document.getElementById('new_type').value;
 let name=document.getElementById('new_name').value.trim()||(type==='woo'?'WooCommerce':'Client');
 let myPairings=(await api('/api/pairings')).pairings||{{}};
 let cc=Object.values(myPairings).filter(p=>p.type==='client').length;
 let max=(await api('/api/pairings')).max_clients||3;
 if(type==='client'&&cc>=max){{document.getElementById('pair_msg').className='msg err';document.getElementById('pair_msg').textContent='{t["limit_reached"]} ('+max+')';return}}
 let r=await fetch('/api/pairings?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok,type:type,name:name,status:'approved'}})}});
 if(r.ok){{document.getElementById('new_tok').value='';document.getElementById('new_name').value='';document.getElementById('pair_msg').className='msg ok';document.getElementById('pair_msg').textContent='{t["added"]}'}}
 else{{let d=await r.json();document.getElementById('pair_msg').className='msg err';document.getElementById('pair_msg').textContent=d.error||'Failed'}}
 refresh();
}}
async function delPairing(tok){{if(!confirm('{t["confirm_delete"]} '+tok+'?'))return;await fetch('/api/forget?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok}})}});refresh()}}
async function addRoute(){{
 let w=document.getElementById('rt_woo').value,c=document.getElementById('rt_client').value,p=document.getElementById('rt_printer').value,tpl=document.getElementById('rt_template').value;
 if(!w||!c){{document.getElementById('route_msg').className='msg err';document.getElementById('route_msg').textContent='{t["select_both"]}';return}}
 let body={{woo_token:w,client:c,printer:p,template:tpl}};
 if(_customTpl)body.template_override=_customTpl;
 let r=await fetch('/api/routes?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
 if(r.ok){{document.getElementById('route_msg').className='msg ok';document.getElementById('route_msg').textContent='{t["route_added"]}';clearRouteTpl()}}
 else{{document.getElementById('route_msg').className='msg err';document.getElementById('route_msg').textContent='{t["route_failed"]}'}}
 refresh();
}}
async function delRoute(i){{await api('/api/routes/'+i,{{method:'DELETE'}});refresh()}}
// -- templates --
let _allTpls={{}};
async function loadTpls(){{
 let r=await api('/api/templates');_allTpls=r.templates||{{}};
 let cats=new Set();Object.entries(_allTpls).forEach(([p,t])=>cats.add(t.industry));
 let tabs=document.getElementById('tmpl_tabs');
 tabs.innerHTML='<span class="tmpl-tab active" onclick="showTpl(\\'all\\')">{t["templates_all"]}</span>';
 cats.forEach(c=>tabs.innerHTML+='<span class="tmpl-tab" onclick="showTpl(\\''+c+'\\')">'+c+'</span>');
 showTpl('all');
 // 也填充路由配置的模板下拉
 let ts=document.getElementById('rt_template'),tsv=ts.value;
 ts.innerHTML='<option value="">— 默认 —</option>';
 Object.entries(_allTpls).forEach(([p,t])=>ts.add(new Option(t.name+' ('+t.industry+')',p)));
 ts.value=tsv;
 // 也填充测试打印的模板下拉
 let tt=document.getElementById('test_template'),ttv=tt.value;
 tt.innerHTML='<option value="">— 默认 —</option>';
 Object.entries(_allTpls).forEach(([p,t])=>tt.add(new Option(t.name+' ('+t.industry+')',p)));
 tt.value=ttv;
}}
function showTpl(cat){{
 document.querySelectorAll('.tmpl-tab').forEach(e=>e.classList.toggle('active',e.textContent===cat||(cat==='all'&&e.textContent==='{t["templates_all"]}')));
 let grid=document.getElementById('tmpl_grid');
 let e=Object.entries(_allTpls);
 if(cat!=='all')e=e.filter(([p,t])=>t.industry===cat);
 if(!e.length){{grid.innerHTML='<span class="empty">{t["no_history"]}</span>';return}}
 grid.innerHTML=e.map(([p,t])=>'<div class="tmpl-card" onclick="previewTpl(\\''+p+'\\')"><div class="tname">'+t.name+'</div><div class="tdesc">'+t.desc+'</div><span class="tind">'+t.industry+'</span></div>').join('');
}}
function previewTpl(path){{let t=_allTpls[path];document.getElementById('tmpl_pname').textContent=t.name+' ('+path+')';document.getElementById('tmpl_pbody').textContent=JSON.stringify(t,null,2);document.getElementById('tmpl_preview').classList.add('show')}}
function closePreview(){{document.getElementById('tmpl_preview').classList.remove('show')}}

// -- template override (shared editor, route + test) --
let _customTpl=null, _testCustomTpl=null, _editTarget='route';
function _tplBtnId(){{return _editTarget==='test'?'btn_test_edit_tpl':'btn_edit_tpl'}}
function _tplVar(){{return _editTarget==='test'?_testCustomTpl:_customTpl}}
function _setTplVar(v){{if(_editTarget==='test')_testCustomTpl=v;else _customTpl=v}}
function _tplSelectId(){{return _editTarget==='test'?'test_template':'rt_template'}}
async function editTplForRoute(){{_editTarget='route';await _editTpl()}}
async function editTestTpl(){{_editTarget='test';await _editTpl()}}
async function _editTpl(){{
 let path=document.getElementById(_tplSelectId()).value;
 if(!path){{alert('{t["pick_tpl_first"]}');return}}
 let d=await api('/api/templates/'+path);
 document.getElementById('tpl_edit_name').textContent=_allTpls[path]?(_allTpls[path].name+' ('+_allTpls[path].industry+')'):path;
 document.getElementById('tpl_edit_body').value=_tplVar()||JSON.stringify(d,null,2);
 document.getElementById('tpl_editor').classList.add('show');
}}
function closeTplEditor(){{document.getElementById('tpl_editor').classList.remove('show')}}
function saveRouteTpl(){{
 _setTplVar(document.getElementById('tpl_edit_body').value);
 closeTplEditor();
 let btn=document.getElementById(_tplBtnId());
 if(btn){{btn.style.background='#ff9800';btn.style.color='#fff';btn.textContent='✏️✓'}}
}}
function clearRouteTpl(){{
 _setTplVar(null);
 closeTplEditor();
 let btn=document.getElementById(_tplBtnId());
 if(btn){{btn.style.background='';btn.style.color='';btn.textContent='✏️'}}
}}

async function onTestRouteChange(){{
 let ri=document.getElementById('test_route').value;
 if(ri==='')return;
 let rd=await api('/api/routes');let r=rd.routes[parseInt(ri)];
 if(!r)return;
 let tt=document.getElementById('test_template');
 if(r.template)tt.value=r.template; else tt.value='';
 // pull custom override from route
 if(r.template_override){{_testCustomTpl=r.template_override;let btn=document.getElementById('btn_test_edit_tpl');if(btn){{btn.style.background='#ff9800';btn.style.color='#fff';btn.textContent='✏️✓'}}}}
 else{{_testCustomTpl=null;let btn=document.getElementById('btn_test_edit_tpl');if(btn){{btn.style.background='';btn.style.color='';btn.textContent='✏️'}}}}
}}


async function sendTest(){{
 let ri=document.getElementById('test_route').value;
 if(ri===''){{document.getElementById('test_msg').className='msg err';document.getElementById('test_msg').textContent='{t["select_route_first"]}';return}}
 let rd=await api('/api/routes');let r=rd.routes[parseInt(ri)];
 if(!r){{document.getElementById('test_msg').className='msg err';document.getElementById('test_msg').textContent='{t["route_not_found"]}';return}}
 let raw=document.getElementById('test_items').value;
 let items=[];raw.split('|').forEach(p=>{{let x=p.split(',');if(x.length>=2)items.push({{name:x[0].trim(),price:parseFloat(x[1]),quantity:parseInt(x[2]||1)}})}});
 let tot=items.reduce((s,i)=>s+(i.price||0)*(i.quantity||1),0).toFixed(2);
 let testTpl=document.getElementById('test_template').value;
 let bodyObj={{number:document.getElementById('test_order').value,date_created:new Date().toISOString(),payment_method_title:'Test',total:tot,line_items:items,shipping_total:'0.00'}};
 if(testTpl)bodyObj._template=testTpl;
 if(_testCustomTpl)bodyObj._template_override=_testCustomTpl;
 let body=JSON.stringify(bodyObj);
 let hdrs={{'Content-Type':'application/json','X-Print-Client':r.client,'X-Printer-Name':r.printer}};
 let res=await fetch('/wc?token='+(r.woo_token||''),{{method:'POST',headers:hdrs,body:body}});
 let msg=document.getElementById('test_msg');
 try{{let j=await res.json();if(res.ok){{msg.className='msg ok';msg.textContent='{t["sent"]} → '+JSON.stringify(j)}}else{{msg.className='msg err';msg.textContent=j.error||j.detail||'Failed'}}}}
 catch(e){{msg.className='msg err';msg.textContent='{t["server_error"]}: '+res.status}}
 refresh();
}}
refresh();setInterval(refresh,10000);loadTpls();
</script></body></html>"""

def admin_page(lang):
    t = T[lang]; css = _page_css()
    return f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · {t['admin_title']}</title><style>{css}</style></head><body>
<div class="header"><div class="logo">🖨️ <span>Print Relay</span></div>{_lang_nav(lang)}</div>
<div class="container">
<h1>🔐 Print Relay · {t['admin_title']}</h1>
<div class="card"><h2>📋 {t['admin_pending']}</h2><div id="pending" class="empty">{t['loading']}</div></div>
<div class="card"><h2>👥 {t['admin_all']}</h2><div id="all_accounts" class="empty">{t['loading']}</div><div id="admin_msg" class="msg"></div></div>
</div>
{_lang_script()}
<script>
const T=new URLSearchParams(location.search).get('token')||'';
async function api(p,o={{}}){{let s=p.includes('?')?'&':'?';let r=await fetch(p+s+'token='+T,o);return r.json()}}
async function refresh(){{
 let d=await api('/admin/accounts');let pending='',all='';
 if(!d.accounts||Object.keys(d.accounts).length===0){{pending='';all=''}}
 else{{
  let pc=0;
  for(let [tok,a] of Object.entries(d.accounts)){{
   let sts={{pending:'{t["admin_status_pending"]}',active:'{t["admin_status_active"]}',suspended:'{t["admin_status_suspended"]}'}}[a.status]||a.status;
   let st_t='<span class="tag '+a.status+'">'+sts+'</span>';
   let clients=Object.values(a.pairings||{{}}).filter(p=>p.type==='client').length;
   let max=a.limits?.max_clients||3;
   let acts='';
   if(a.status==='pending'){{acts='<button class="btn approve" onclick="act(\\''+tok+'\\',\\'approve\\')">{t["approve"]}</button><button class="btn reject" onclick="act(\\''+tok+'\\',\\'reject\\')">{t["reject"]}</button>';pc++}}
   else if(a.status==='active')acts='<button class="btn suspend" onclick="setStatus(\\''+tok+'\\',\\'suspended\\')">{t["suspend"]}</button>';
   else if(a.status==='suspended')acts='<button class="btn activate" onclick="setStatus(\\''+tok+'\\',\\'active\\')">{t["resume"]}</button>';
   acts+='<span class="limits"> {t["limit_label"]} <input id="lim_'+tok+'" value="'+max+'" size="2"> <button class="btn activate" onclick="setLimit(\\''+tok+'\\')">{t["save"]}</button></span>';
   if(a.status==='pending')pending+=`<tr><td>${{a.email}}</td><td>${{a.name||'—'}}</td><td>${{a.created?.slice(0,10)||''}}</td><td>${{acts}}</td></tr>`;
   all+=`<tr><td>${{a.email}}</td><td>${{a.name||'—'}}</td><td>${{st_t}}</td><td>${{clients}}/${{max}}</td><td>${{a.created?.slice(0,16)||''}}</td><td>${{acts}}</td></tr>`;
  }}
  pending=(pc===0?'<span class="empty">{t["no_pending"]}</span>':'<table><tr><th>{t["admin_email"]}</th><th>{t["admin_name"]}</th><th>{t["admin_registered"]}</th><th>{t["admin_action"]}</th></tr>')+pending+(pc>0?'</table>':'');
  all='<table><tr><th>{t["admin_email"]}</th><th>{t["admin_name"]}</th><th>{t["admin_status"]}</th><th>{t["clients_label"]}</th><th>{t["admin_registered"]}</th><th>{t["admin_action"]}</th></tr>'+all+'</table>';
 }}
 document.getElementById('pending').innerHTML=pending;document.getElementById('all_accounts').innerHTML=all;
}}
async function act(tok,type){{let ep=type==='approve'?'/admin/approve':'/admin/reject';await fetch(ep+'?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok}})}});refresh()}}
async function setStatus(tok,s){{await fetch('/admin/status?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok,status:s}})}});refresh()}}
async function setLimit(tok){{let v=parseInt(document.getElementById('lim_'+tok).value)||3;await fetch('/admin/limits?token='+T,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:tok,max_clients:v}})}});document.getElementById('admin_msg').className='msg ok';document.getElementById('admin_msg').textContent='{t["updated"]}';refresh()}}
refresh();setInterval(refresh,15000);
</script></body></html>"""

REGISTER_PAGE_SIMPLE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Relay · Token</title><style>
body{font:14px/1.5 system-ui;background:#f5f5f5;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:10px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);max-width:400px;text-align:center}
h1{font-size:20px;margin-bottom:8px}.sub{color:#666;font-size:13px;margin-bottom:20px}
.code{font:bold 32px monospace;background:#e3f2fd;padding:12px 24px;border-radius:8px;letter-spacing:3px;margin:16px 0;display:inline-block}
.help{font-size:12px;color:#999;margin-top:16px}.btn{padding:8px 20px;background:#1976d2;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;margin-top:12px}
</style></head><body><div class="card">
<h1>🖨️ Print Relay</h1><div class="sub">Ihr Token / Your Token:</div>
<div class="code">__TOKEN__</div>
<p class="help">In Control Panel einfugen / Paste into Control Panel</p>
<a href="/login" class="btn">Zum Login / Login</a>
</div></body></html>"""

# ── HTTP Handler ─────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): log.debug(f"HTTP {args}")

    def _get_token(self):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query).get('token', [''])[0]

    def _get_lang(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        lang = qs.get('lang', [''])[0]
        if lang not in ('zh', 'en'): lang = 'en'
        return lang

    def _get_account(self, token):
        if not token: return None, None
        with state.lock:
            if token in state.accounts: return token, state.accounts[token]
        return None, None

    def _is_admin(self, token): return token == ADMIN_TOKEN

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        token = self._get_token(); lang = self._get_lang()

        if path == '/health': self._json({'ok': True}); return
        if path == '/editor':
            try: self._html(_load_editor_html()); return
            except: self.send_error(404); return
        if path == '/': self._html(landing_page(lang)); return
        if path == '/login': self._html(login_page(lang)); return

        if path == '/register':
            tok = secrets.token_hex(4)
            with state.lock:
                admin = state.accounts.get(ADMIN_TOKEN, {})
                admin.setdefault('pairings', {})[tok] = {"type":"woo","name":f"Woo-{tok[:4]}","status":"pending","created_at":datetime.now(timezone.utc).isoformat()}
                state.save()
            self._html(REGISTER_PAGE_SIMPLE.replace('__TOKEN__', tok)); return

        if path == '/admin' and self._is_admin(token):
            self._html(admin_page(lang)); return

        if path == '/admin/accounts' and self._is_admin(token):
            with state.lock: self._json({'accounts': state.accounts}); return

        acct_tok, acct = self._get_account(token)
        if not acct:
            if path in ('/', '/panel'): self._html(landing_page(lang)); return
            self._json({'error': 'unauthorized'}, 403); return

        if path == '/panel': self._html(panel_page(lang, token)); return

        if path == '/api/pairings':
            pairings = acct.get('pairings', {})
            cc = sum(1 for p in pairings.values() if p.get('type') == 'client')
            self._json({'pairings': pairings, 'account_name': acct.get('name',''), 'account_email': acct.get('email',''), 'client_count': cc, 'max_clients': acct.get('limits',{}).get('max_clients',3)}); return

        if path == '/api/routes': self._json({'routes': acct.get('routes',[])}); return
        if path == '/api/history': self._json({'history': acct.get('history',[])}); return
        if path == '/api/templates':
            import glob as _g
            tdir = DATA_DIR / 'templates'
            if not tdir.is_dir(): self._json({'templates': {}}); return
            templates = {}
            for f in sorted(tdir.glob('**/*.json')):
                try:
                    rel = str(f.relative_to(tdir))
                    d = json.loads(f.read_text())
                    templates[rel] = {'name': d.get('name',''), 'industry': d.get('industry',''), 'desc': d.get('desc','')}
                except: pass
            self._json({'templates': templates}); return
        if path.startswith('/api/templates/') and len(path) > 15:
            tpath = DATA_DIR / 'templates' / path[15:].lstrip('/')
            if tpath.is_file():
                try:
                    d = json.loads(tpath.read_text())
                    self._json(d); return
                except: pass
            self._json({'error': 'not found'}, 404); return
        self.send_error(404)

    def do_POST(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        token = self._get_token()
        cl = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(cl)) if cl else {}

        if path == '/api/register':
            email = body.get('email','').strip(); password = body.get('password','')
            if not email or len(password) < 6: self._json({'error': 'Email required, password min 6 chars'}, 400); return
            with state.lock:
                for acct in state.accounts.values():
                    if acct.get('email') == email: self._json({'error': 'Email already registered'}, 409); return
                acct_token = _new_account_token()
                state.accounts[acct_token] = {"email":email,"password_hash":_hash_password(password),"name":"","status":"pending","limits":{"max_clients":3},"pairings":{},"routes":[],"history":[],"created":datetime.now(timezone.utc).isoformat()}
                state.save()
            log.info(f"New registration: {email}"); self._json({'ok': True}); return

        if path == '/api/login':
            email = body.get('email','').strip(); password = body.get('password','')
            if email == 'admin' and password == ADMIN_TOKEN: self._json({'token': ADMIN_TOKEN}); return
            with state.lock:
                for atok, acct in state.accounts.items():
                    if acct.get('email') == email:
                        if acct.get('status') == 'suspended': self._json({'error': 'Account suspended'}, 403); return
                        if _check_password(password, acct['password_hash']): self._json({'token': atok}); return
                        self._json({'error': 'Wrong password'}, 401); return
            self._json({'error': 'Email not registered'}, 404); return

        if path.startswith('/wc'):
            woo_token = token; found_acct = None
            with state.lock:
                for atok, acct in state.accounts.items():
                    if woo_token in acct.get('pairings',{}): found_acct = atok; break
            if not found_acct: self._json({'error': 'unauthorized'}, 403); return
            target_client = self.headers.get('X-Print-Client',''); target_printer = self.headers.get('X-Printer-Name','')
            cut_per_item = self.headers.get('X-Cut-Per-Item','') == '1'
            if not target_client:
                order = body
                if cut_per_item: order = {**order, 'cut_per_item': True}
                with state.lock:
                    acct = state.accounts[found_acct]; routes = acct.get('routes',[])
                matched = [r for r in routes if r.get('woo_token') == woo_token]
                if not matched: self._json({'error':'No route'}, 503); return
                results = []
                for r in matched:
                    cli = r.get('client',''); prn = r.get('printer','')
                    result = load_and_render_any(r, order)
                    if not result or not result[0]:
                        results.append({'client':cli,'printer':prn,'ok':False,'error':'template not found'})
                        continue
                    data_b64, fmt = result
                    payload = json.dumps({"printer":prn,"ticket_b64":data_b64} if fmt == 'escpos' else {"printer":prn,"pdf_b64":data_b64}).encode()
                    ok = run_async(send_to_client(cli, payload))
                    results.append({'client':cli,'printer':prn,'ok':ok})
                    if ok:
                        with state.lock:
                            a = state.accounts[found_acct]
                            a.setdefault('history',[]).insert(0,{"time":datetime.now(timezone.utc).isoformat(),"client":cli,"printer":prn or "default","order":str(order.get('number',order.get('id'))),"items":len(order.get('line_items',[]))})
                            if len(a['history']) > 50: a['history'].pop()
                with state.lock: state.save()
                self._json({'status':'ok','results':results}); return
            else:
                order = {**body}
                if cut_per_item: order['cut_per_item'] = True
                # 测试打印: 用 body 中的 _template 或默认 kitchen
                tpl_path = body.get('_template', 'restaurant/kitchen.json')
                tpl_override = body.get('_template_override', '')
                route = {'template': tpl_path, 'template_override': tpl_override}
                result = load_and_render_any(route, order)
                if not result or not result[0]:
                    self._json({'error':'template not found: '+tpl_path}, 404); return
                data_b64, fmt = result
                payload = json.dumps({"printer":target_printer,"ticket_b64":data_b64} if fmt == 'escpos' else {"printer":target_printer,"pdf_b64":data_b64}).encode()
                ok = run_async(send_to_client(target_client, payload))
                results = [{'client':target_client,'printer':target_printer,'ok':ok}]
                if ok:
                    with state.lock:
                        a = state.accounts[found_acct]
                        a.setdefault('history',[]).insert(0,{"time":datetime.now(timezone.utc).isoformat(),"client":target_client,"printer":target_printer or "default","order":str(body.get('number','TEST')),"items":len(body.get('line_items',[]))})
                        if len(a['history']) > 50: a['history'].pop()
                with state.lock: state.save()
                self._json({'status':'ok','results':results}); return

        if self._is_admin(token):
            if path == '/admin/approve':
                u_tok = body.get('token','')
                with state.lock:
                    if u_tok in state.accounts: state.accounts[u_tok]['status'] = 'active'; state.save()
                self._json({'ok':True}); return
            if path == '/admin/reject':
                u_tok = body.get('token','')
                with state.lock:
                    if u_tok in state.accounts: state.accounts[u_tok]['status'] = 'rejected'; state.save()
                self._json({'ok':True}); return
            if path == '/admin/status':
                u_tok = body.get('token',''); s = body.get('status','')
                with state.lock:
                    if u_tok in state.accounts and s in ('active','suspended'): state.accounts[u_tok]['status'] = s; state.save()
                self._json({'ok':True}); return
            if path == '/admin/limits':
                u_tok = body.get('token',''); mx = body.get('max_clients')
                with state.lock:
                    if u_tok in state.accounts and isinstance(mx,int) and mx > 0: state.accounts[u_tok].setdefault('limits',{})['max_clients'] = mx; state.save()
                self._json({'ok':True}); return

        acct_tok, acct = self._get_account(token)
        if not acct: self._json({'error':'unauthorized'},403); return

        if path == '/api/approve' and acct.get('status') == 'active':
            ptok = body.get('token','')
            with state.lock:
                if ptok in acct.get('pairings',{}): acct['pairings'][ptok]['status'] = 'approved'; state.save()
            self._json({'ok':True}); return
        if path == '/api/reject':
            ptok = body.get('token','')
            with state.lock:
                if ptok in acct.get('pairings',{}): acct['pairings'][ptok]['status'] = 'rejected'; state.save()
            self._json({'ok':True}); return
        if path == '/api/forget':
            ptok = body.get('token','')
            with state.lock:
                acct['pairings'].pop(ptok,None)
                acct['routes'] = [r for r in acct.get('routes',[]) if r.get('woo_token') != ptok and r.get('client') != ptok]
                state.save()
            self._json({'ok':True}); return
        if path == '/api/routes':
            with state.lock:
                acct.setdefault('routes',[]).append({"woo_token":body.get('woo_token',''),"client":body.get('client',''),"printer":body.get('printer',''),"template":body.get('template',''),"template_override":body.get('template_override','')})
                state.save()
            self._json({'ok':True}); return
        if path == '/api/pairings':
            ptok = body.get('token',''); ptype = body.get('type','woo')
            if not ptok or len(ptok) < 4: self._json({'error':'Token too short'},400); return
            pairings = acct.get('pairings',{})
            if ptype == 'client':
                cc = sum(1 for p in pairings.values() if p.get('type') == 'client')
                mx = acct.get('limits',{}).get('max_clients',3)
                if cc >= mx: self._json({'error':f'Client limit ({mx})'},403); return
            with state.lock:
                acct.setdefault('pairings',{})[ptok] = {"type":ptype,"name":body.get('name',f"{ptype}-{ptok[:4]}"),"status":"approved","created_at":datetime.now(timezone.utc).isoformat()}
                state.save()
            self._json({'ok':True,'token':ptok}); return
        if path == '/api/register':
            ptok = body.get('token',''); ptype = body.get('type','woo'); name = body.get('name',f"{ptype}-{ptok[:4]}")
            with state.lock:
                if ptok not in acct.get('pairings',{}): acct.setdefault('pairings',{})[ptok] = {"type":ptype,"name":name,"status":"approved","created_at":datetime.now(timezone.utc).isoformat()}; state.save()
            self._json({'ok':True,'token':ptok}); return
        self.send_error(404)

    def do_DELETE(self):
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path; token = self._get_token()
        if path.startswith('/api/routes/') and token:
            idx = int(path.split('/')[-1])
            acct_tok, acct = self._get_account(token)
            if not acct: self._json({'error':'unauthorized'},403); return
            with state.lock:
                routes = acct.get('routes',[])
                if 0 <= idx < len(routes): routes.pop(idx); state.save()
            self._json({'ok':True}); return
        self.send_error(404)

    def _json(self, data, code=200):
        self.send_response(code); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html(self, html, code=200):
        self.send_response(code); self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store, no-cache, must-revalidate'); self.end_headers()
        self.wfile.write(html.encode())

def run_http():
    HTTPServer(('0.0.0.0', HTTP_PORT), WebhookHandler).serve_forever()

async def main():
    global main_loop; main_loop = asyncio.get_running_loop()
    tcp = await asyncio.start_server(handle_client, '0.0.0.0', TCP_PORT)
    log.info(f"TCP :{TCP_PORT}  HTTP :{HTTP_PORT}")
    Thread(target=run_http, daemon=True).start()
    async with tcp: await tcp.serve_forever()

if __name__ == '__main__':
    log.info("Print Relay v3 starting...")
    asyncio.run(main())
