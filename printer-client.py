#!/usr/bin/env python3
"""
Print Relay Client v4 — JSON 模板版
启动 → 读 config.ini → 扫描打印机 → 管理员在面板添加 → 自动连接
服务器发 JSON → 客户端匹配打印机 → 读对应 JSON 模板 → ESC/POS → 打印机
每台打印机独立模板，config.ini 配置 station_filter、cut_per_item 等。
"""

import socket, struct, json, os, sys, time, threading, secrets, logging, configparser
from datetime import datetime
from pathlib import Path

# ── 硬编码 ───────────────────────────────────────────────────
RELAY_HOST = "relay.thecarte.eu"
RELAY_PORT = 51900
PAPER_WIDTH = 80

# 配置文件均在 EXE 目录下
_EXE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
INI_FILE = _EXE_DIR / "config.ini"
TEMPLATES_DIR = _EXE_DIR / "templates"

def load_config():
    """读取 config.ini"""
    cfg = configparser.ConfigParser()
    if INI_FILE.exists():
        cfg.read(INI_FILE, encoding='utf-8')
        log.info(f"已加载配置: {len(cfg.sections())} 个打印机分区")
    else:
        log.warning(f"未找到 {INI_FILE} — 请将 config.ini 放到 EXE 同目录")
    return cfg

def find_printer_section(config, printer_name):
    """根据打印机名匹配 [printer.*] 分区"""
    for section in config.sections():
        if section.startswith('printer.'):
            name = config.get(section, 'name', fallback='')
            if name == printer_name:
                return section
    return None

def load_template(config, section):
    """读取该分区的 JSON 模板"""
    tpl_path = config.get(section, 'template', fallback=None)
    if not tpl_path:
        return None
    # 支持相对路径（相对于 EXE 目录）
    p = Path(tpl_path)
    if not p.is_absolute():
        p = _EXE_DIR / p
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.error(f"无法加载模板 {p}: {e}")
        return None

CONFIG_DIR = Path(os.getenv('APPDATA', os.path.expanduser('~'))) / 'PrintRelay'
CONFIG_FILE = CONFIG_DIR / 'config.json'
LOG_FILE = CONFIG_DIR / 'client.log'
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()])
log = logging.getLogger('printer-client')

CLIENT_NAME = os.environ.get('COMPUTERNAME', socket.gethostname())

# ── 配对 Token ──────────────────────────────────────────────
def load_or_create_token():
    try:
        if CONFIG_FILE.exists():
            d = json.loads(CONFIG_FILE.read_text())
            return d.get('token', ''), d
    except: pass
    return '', {}

def save_token(token, extra=None):
    d = extra or {}
    d['token'] = token
    CONFIG_FILE.write_text(json.dumps(d, indent=2), encoding='utf-8')

# ── 打印机 ──────────────────────────────────────────────────
def list_printers():
    """全量枚举：三路合并去重，不短路"""
    try:
        import win32print
        seen = set()

        def add(lst):
            for p in lst:
                if p and p not in seen:
                    seen.add(p)

        # 方法1: 本地 + 网络 (level 1)
        try:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            add(p['pPrinterName'] for p in win32print.EnumPrinters(flags, None, 1) if p.get('pPrinterName'))
        except Exception as e:
            log.warning(f"EnumPrinters level 1 失败: {e}")

        # 方法2: 本地 (level 2，含虚拟打印机)
        try:
            add(p['pPrinterName'] for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL, None, 2) if p.get('pPrinterName'))
        except Exception as e:
            log.warning(f"EnumPrinters level 2 失败: {e}")

        # 方法3: 默认打印机兜底
        try:
            def_printer = win32print.GetDefaultPrinter()
            if def_printer:
                add([def_printer])
        except Exception as e:
            log.warning(f"GetDefaultPrinter 失败: {e}")

        printers = sorted(seen)  # 排序，稳定顺序
        if printers:
            log.info(f"检测到 {len(printers)} 台打印机: {printers}")
        else:
            log.warning("未检测到任何打印机！请检查: 1) 打印后台服务是否运行 2) 是否安装了打印机驱动")
        return printers
    except ImportError:
        log.error("pywin32 未正确安装，无法枚举打印机")
        return []

def send_raw(printer, data):
    try:
        import win32print
        h = win32print.OpenPrinter(printer)
        try:
            win32print.StartDocPrinter(h, 1, ("PrintRelay", None, "RAW"))
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
            win32print.EndDocPrinter(h)
            return True
        finally: win32print.ClosePrinter(h)
    except Exception as e:
        log.error(f"打印失败: {e}"); return False


# ── ESC/POS 渲染引擎 ──────────────────────────────────────────
def escpos_init():
    return b'\x1b\x40'

def escpos_align(align='left'):
    m = {'left': 0, 'center': 1, 'right': 2}
    return b'\x1b\x61' + bytes([m.get(align, 0)])

def escpos_bold(on=True):
    return b'\x1b\x45' + bytes([1 if on else 0])

def escpos_cut():
    return b'\n\n\n\n\x1d\x56\x00'

def escpos_line(text, width=80):
    n = 48 if width >= 80 else 32
    return text.encode('latin-1', errors='replace')[:n] + b'\n'

def escpos_div(char='-', width=80):
    n = 48 if width >= 80 else 32
    return (char * n).encode()[:n] + b'\n'

def escpos_feed(n=1):
    return b'\n' * n

# ── JSON 模板渲染引擎 ────────────────────────────────────────
import re as _re

# 变量映射：模板变量 → WooCommerce 订单字段访问方式
VAR_MAP = {
    'restaurant_name': 'Restaurant Asia Shanghai',
    'id': lambda o: str(o.get('number', o.get('id', '?'))),
    'table_id': lambda o: str(o.get('table_id', o.get('table', '?'))),
    'table': lambda o: str(o.get('table', o.get('table_id', '?'))),
    'printed_at': lambda o: datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'total_amount': lambda o: str(o.get('total', '0')),
    'qty': lambda item: str(item.get('quantity', 1)),
    'item_code': lambda item: str(item.get('sku', item.get('id', ''))),
    'name': lambda item: str(item.get('name', '')),
    'price': lambda item: str(item.get('price', '0')),
    'total': lambda item: str(item.get('total', item.get('price', '0'))),
}

def _resolve_vars(template_str, order, item=None):
    """替换 {{var}} 为实际值"""
    def repl(m):
        key = m.group(1)
        if key in VAR_MAP:
            v = VAR_MAP[key]
            if callable(v):
                return v(item) if item is not None else v(order)
            return str(v)
        return m.group(0)
    return _re.sub(r'\{\{(\w+)\}\}', repl, template_str)

def escpos_size(on, size=''):
    """size: xl=双高双宽, wide=双宽"""
    if not on or not size:
        return b''
    if size == 'xl':
        return b'\x1d\x21\x11'  # 双高双宽
    elif size == 'wide':
        return b'\x1d\x21\x10'  # 双宽
    return b''

def escpos_size_off():
    return b'\x1d\x21\x00'

def escpos_line_len(text, width_mm):
    """根据 mm 宽度计算字符数（热敏纸 ~2 chars/mm）"""
    n = int(width_mm * 1.8)  # 48mm → ~86 chars, 42mm → ~75 chars
    return text.encode('latin-1', errors='replace')[:n] + b'\n'

def render_json_template(template, order, config, section):
    """用户 JSON 模板格式 → ESC/POS 字节"""
    width_mm = template.get('width', 48)
    out = escpos_init()

    lines = template.get('lines', [])
    items = order.get('line_items', [])

    # station_filter
    flt = config.get(section, 'station_filter', fallback=None) if section else None
    if flt:
        items = [i for i in items if flt.lower() in i.get('name', '').lower() or flt.lower() in i.get('categories', [])]

    for el in lines:
        if 'hr' in el:
            ch = el.get('hr', '-')
            n = int(width_mm * 1.8)
            out += (ch * n).encode()[:n] + b'\n'
            continue

        if 'repeat' in el and el['repeat'] == 'items':
            for item in items:
                sz = el.get('size', '')
                bold = el.get('bold', False)

                if sz:
                    out += escpos_size(True, sz)
                if bold:
                    out += escpos_bold(True)

                if 'text' in el:
                    txt = _resolve_vars(el['text'], order, item)
                    al = el.get('align', 'left')
                    if al == 'center':
                        out += escpos_align('center')
                    elif al == 'right':
                        out += escpos_align('right')
                    out += escpos_line_len(txt, width_mm)
                elif 'left' in el or 'right' in el:
                    left = _resolve_vars(el.get('left', ''), order, item)
                    right = _resolve_vars(el.get('right', ''), order, item)
                    n = int(width_mm * 1.8)
                    txt = left.ljust(n - len(right)) + right
                    out += escpos_line_len(txt, width_mm)

                if sz:
                    out += escpos_size_off()
                if bold:
                    out += escpos_bold(False)
            continue

        if 'text' in el:
            txt = _resolve_vars(el['text'], order)
            al = el.get('align', 'left')
            bold = el.get('bold', False)
            sz = el.get('size', '')

            if sz:
                out += escpos_size(True, sz)
            if bold:
                out += escpos_bold(True)

            if al == 'center':
                out += escpos_align('center')
            elif al == 'right':
                out += escpos_align('right')

            out += escpos_line_len(txt, width_mm)

            if sz:
                out += escpos_size_off()
            if bold:
                out += escpos_bold(False)

    return out


# ── 客户端核心 ───────────────────────────────────────────────
class Client:
    def __init__(self):
        self.token, _ = load_or_create_token()
        if not self.token:
            self.token = secrets.token_hex(4)  # 8 位 hex
            save_token(self.token)
        self.config = load_config()
        self.running = False
        self.state = 'idle'
        self.printers = []    # 当前打印机列表
        self._sock = None     # TCP socket（连接后设）
        self._sock_lock = threading.Lock()
        self.on_state = None

    def scan_printers(self):
        """重新扫描打印机，若已连接则通知服务器"""
        self.printers = list_printers()
        if self.printers:
            log.info(f"扫描完成: {self.printers}")
        else:
            log.warning("扫描完成: 未检测到打印机")
        # 如果已连接，通知服务器更新
        with self._sock_lock:
            if self._sock:
                try:
                    info = json.dumps({"paper_width": PAPER_WIDTH, "printers": self.printers})
                    self._sock.sendall(info.encode() + b"\n")
                    log.info("已通知服务器打印机列表更新")
                except Exception as e:
                    log.warning(f"通知服务器失败: {e}")
        return self.printers

    def start(self): 
        if self.running: return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try: self._run()
            except Exception as e: log.error(f"循环异常: {e}")
            if self.running:
                self._state('connecting', '5 秒后重连...')
                for _ in range(5):
                    if not self.running: return
                    time.sleep(1)

    def _run(self):
        self._state('connecting', f'连接中 {RELAY_HOST}:{RELAY_PORT}...')

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((RELAY_HOST, RELAY_PORT))

        # TCP keepalive — 防 NAT 空闲超时断连
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except: pass

        # REGISTER
        sock.sendall(f"REGISTER {self.token}\n".encode())

        # 发送打印机列表
        if not self.printers:
            self.printers = list_printers()
        if not self.printers:
            self._state('warning', '未检测到打印机！请检查打印后台服务')
        info = json.dumps({"paper_width": PAPER_WIDTH, "printers": self.printers})
        sock.sendall(info.encode() + b"\n")

        # 等待服务器响应
        resp = sock.recv(1024).strip()
        if resp == b'UNKNOWN_TOKEN':
            self._state('error', '未知 Token — 请在控制面板添加此配对码')
            sock.close(); return
        if resp == b'WRONG_TYPE':
            self._state('error', 'Token 类型不是客户端')
            sock.close(); return

        if resp == b'PENDING':
            self._state('pending', f'等待面板审批... 配对码: {self.token}')
            while self.running:
                try:
                    r2 = sock.recv(1024).strip()
                    if r2 == b'APPROVED':
                        self._state('approved', '已审批！打印机就绪。')
                        break
                except: break
        elif resp == b'APPROVED':
            self._state('approved', '已连接并审批')
        else:
            self._state('error', f'未知响应: {resp}')
            sock.close(); return

        # 正常模式 — 接收打印任务
        with self._sock_lock:
            self._sock = sock
        sock.settimeout(60)
        printer_str = ', '.join(self.printers) if self.printers else '无'
        self._state('approved', f'在线 | 打印机: {printer_str}')
        try:
            while self.running:
                try:
                    hdr = self._recv(sock, 4)
                    if not hdr: break
                    length = struct.unpack('>I', hdr)[0]
                    if length == 0: continue   # 心跳包，跳过
                    if length > 512*1024: break
                    data = self._recv(sock, length)
                    if not data: break

                    try:
                        msg = json.loads(data.decode('utf-8'))
                    except:
                        log.warning(f"无效 JSON: {data[:100]}")
                        continue

                    if msg.get('type') != 'print':
                        continue

                    order = msg.get('order', {})
                    target_printer = msg.get('printer', '')

                    # 匹配 config.ini 中的打印机配置
                    sec = find_printer_section(self.config, target_printer)
                    template = load_template(self.config, sec) if sec else None

                    if not template:
                        log.warning(f"无模板匹配: {target_printer}")
                        continue

                    try:
                        ticket = render_json_template(template, order, self.config, sec)
                    except Exception as e:
                        log.error(f"模板渲染失败: {e}")
                        continue

                    printer = target_printer or (self.printers[0] if self.printers else None)
                    if not printer:
                        log.warning("无可用打印机")
                        continue

                    if sec:
                        mode = self.config.get(sec, 'mode', fallback='receipt')
                        feed_n = self.config.getint(sec, 'feed_lines', fallback=0)
                        cut_per = self.config.getboolean(sec, 'cut_per_item', fallback=False)
                    else:
                        mode, feed_n, cut_per = 'receipt', 0, False

                    if mode == 'per_item':
                        # 逐菜切纸
                        for item in order.get('line_items', []):
                            one = {'line_items': [item],
                                   **{k: v for k, v in order.items() if k != 'line_items'}}
                            t = render_json_template(template, one, self.config, sec)
                            if feed_n:
                                t += escpos_feed(feed_n)
                            if cut_per:
                                t += escpos_cut()
                            send_raw(printer, t)
                        onum = order.get('number', '?')
                        self._state('approved', f'per_item #{onum} -> {printer}')
                    else:
                        ok = send_raw(printer, ticket)
                        onum = order.get('number', '?')
                        if ok:
                            self._state('approved', f'已打印 #{onum} ({len(ticket)}B) -> {printer}')
                        else:
                            self._state('error', f'打印失败 #{onum} -> {printer}')
                except socket.timeout:
                    continue
                except:
                    break
        finally:
            with self._sock_lock:
                self._sock = None
            try: sock.close()
            except: pass
            self._state('connecting', '连接断开')

    def _recv(self, sock, n):
        buf = b''
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c: return b''
            buf += c
        return buf

    def _state(self, s, msg):
        self.state = s
        log.info(f"[{s}] {msg}")
        if self.on_state: self.on_state(s, msg)

# ── GUI ──────────────────────────────────────────────────────
# 托盘图标 (ctypes, 纯 stdlib)
# 用窗口过程钩子直接捕获托盘消息 — 比 PeekMessage 轮询可靠，不会被 tkinter 主循环吃掉
import ctypes as _ct
from ctypes import wintypes as _w

_NIM_ADD, _NIM_DELETE, _NIM_MODIFY = 0, 2, 1
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 1, 2, 4
_WM_TRAY = 0x8000 + 1

# 窗口过程钩子 — 全局回调（同一个进程只有这一个托盘）
_WNDPROC_TYPE = _ct.WINFUNCTYPE(_ct.c_void_p, _w.HWND, _ct.c_uint, _w.WPARAM, _w.LPARAM)
_orig_wndproc = None
_tray_menu_handler = None
_tray_dblclick_handler = None

def _tray_wndproc(hwnd, msg, wparam, lparam):
    """窗口过程钩子：截获托盘消息"""
    if msg == _WM_TRAY:
        if lparam == 0x0205:  # WM_RBUTTONUP → 右键菜单
            if _tray_menu_handler:
                _tray_menu_handler()
        elif lparam == 0x0202:  # WM_LBUTTONDBLCLK → 双击还原
            if _tray_dblclick_handler:
                _tray_dblclick_handler()
        return 0
    # 其他消息交给原窗口过程
    return _ct.windll.user32.CallWindowProcA(_orig_wndproc, hwnd, msg, wparam, lparam)

class _NOTIFYICONDATA(_ct.Structure):
    _fields_ = [("cbSize", _w.DWORD), ("hWnd", _w.HWND), ("uID", _w.UINT),
                ("uFlags", _w.UINT), ("uCallbackMessage", _w.UINT),
                ("hIcon", _w.HICON), ("szTip", _w.CHAR * 128)]

def _get_printer_icon():
    """获取系统打印机图标 (SHGetStockIconInfo, Vista+)"""
    SHGSI_ICON = 0x100
    SIID_PRINTER = 16
    
    class _SHSTOCKICONINFO(_ct.Structure):
        _fields_ = [("cbSize", _w.DWORD), ("hIcon", _w.HICON),
                    ("iSysImageIndex", _ct.c_int), ("iIcon", _ct.c_int),
                    ("szPath", _w.CHAR * 260)]
    
    sii = _SHSTOCKICONINFO()
    sii.cbSize = _ct.sizeof(sii)
    _ct.windll.shell32.SHGetStockIconInfo(SIID_PRINTER, SHGSI_ICON, _ct.byref(sii))
    return sii.hIcon

def _tray_add(hwnd, hicon, tip="Print Relay"):
    """添加托盘图标（打印机图标 + 窗口过程钩子）"""
    global _orig_wndproc
    # 安装窗口过程钩子 — 比 PeekMessage 轮询可靠
    GWL_WNDPROC = -4
    _orig_wndproc = _ct.windll.user32.SetWindowLongPtrA(
        hwnd, GWL_WNDPROC,
        _ct.cast(_WNDPROC_TYPE(_tray_wndproc), _ct.c_void_p).value)
    
    nid = _NOTIFYICONDATA()
    nid.cbSize = _ct.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP   # ← 必须有 NIF_ICON + hIcon
    nid.uCallbackMessage = _WM_TRAY
    nid.hIcon = hicon
    nid.szTip = tip.encode('utf-8')[:127]
    return _ct.windll.shell32.Shell_NotifyIconA(_NIM_ADD, _ct.byref(nid))

def _tray_del(hwnd):
    nid = _NOTIFYICONDATA()
    nid.cbSize = _ct.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    return _ct.windll.shell32.Shell_NotifyIconA(_NIM_DELETE, _ct.byref(nid))

class App:
    def __init__(self):
        import tkinter as tk; from tkinter import ttk
        self.tk = tk; self.ttk = ttk
        self.client = Client()
        self.client.on_state = lambda s, m: self.root.after(0, self._on_state, s, m)

        self.root = tk.Tk()
        self.root.title(f"Print Relay - {CLIENT_NAME}")
        self.root.geometry("460x440")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        # 托盘图标
        self._hicon = None
        self.root.after(500, self._init_tray)

        # 开机启动参数
        if '--startup' in sys.argv:
            self.root.withdraw()

        self._build()
        # 启动时先扫描打印机
        self._scan_and_show()
        self.client.start()

    def _build(self):
        t = self.tk; tt = self.ttk

        # 标题
        f = tt.Frame(self.root, padding=16)
        f.pack(fill=t.X)
        tt.Label(f, text="Print Relay Client", font=('Microsoft YaHei', 14, 'bold')).pack()
        tt.Label(f, text=f"PC: {CLIENT_NAME}", foreground='#666').pack()

        tt.Separator(self.root, orient=t.HORIZONTAL).pack(fill=t.X, padx=16)

        # 状态
        sf = tt.Frame(self.root, padding=12)
        sf.pack(fill=t.X)
        self.status_lbl = tt.Label(sf, text="启动中...", font=('Microsoft YaHei', 11))
        self.status_lbl.pack()

        # Token 卡片
        tf = tt.LabelFrame(self.root, text="配对码", padding=14)
        tf.pack(fill=t.X, padx=16, pady=(8,4))

        tok_row = tt.Frame(tf)
        tok_row.pack(fill=t.X)
        self.token_lbl = tt.Label(tok_row, text=self.client.token, font=('Consolas', 22, 'bold'), foreground='#1565c0')
        self.token_lbl.pack(side=t.LEFT, padx=(0,12))
        self.copy_btn = tt.Button(tok_row, text="复制配对码", command=self._copy_token)
        self.copy_btn.pack(side=t.LEFT)
        tt.Label(tf, text="将此配对码填入控制面板完成配对", foreground='#999', font=('Microsoft YaHei', 9)).pack(pady=(8,0))

        # 打印机区域
        pf = tt.LabelFrame(self.root, text="打印机", padding=12)
        pf.pack(fill=t.X, padx=16, pady=(8,4))

        btn_row = tt.Frame(pf)
        btn_row.pack(fill=t.X)
        self.scan_btn = tt.Button(btn_row, text="重新扫描打印机", command=self._scan_and_show)
        self.scan_btn.pack(side=t.LEFT)
        self.tpl_btn = tt.Button(btn_row, text="编辑模板", command=self._edit_template)
        self.tpl_btn.pack(side=t.LEFT, padx=(8,0))

        self.printer_lbl = tt.Label(pf, text="尚未扫描", foreground='#999', font=('Microsoft YaHei', 9))
        self.printer_lbl.pack(pady=(6,0))

        # 日志
        lf = tt.LabelFrame(self.root, text="日志", padding=8)
        lf.pack(fill=t.BOTH, expand=True, padx=16, pady=(4,12))
        self.log_txt = t.Text(lf, height=5, wrap=t.WORD, font=('Consolas', 8), state=t.DISABLED)
        self.log_txt.pack(fill=t.BOTH, expand=True)

    def _scan_and_show(self):
        """扫描打印机并更新界面"""
        self._log("正在扫描打印机...")
        self.scan_btn.config(state='disabled', text="扫描中...")
        # 在后台线程扫描，避免 UI 卡顿
        def do_scan():
            printers = self.client.scan_printers()
            self.root.after(0, lambda: self._on_scan_done(printers))
        threading.Thread(target=do_scan, daemon=True).start()

    def _on_scan_done(self, printers):
        self.scan_btn.config(state='normal', text="重新扫描打印机")
        if printers:
            names = ', '.join(printers)
            self.printer_lbl.config(text=f"已检测: {names}", foreground='#2e7d32')
            self._log(f"检测到 {len(printers)} 台打印机: {names}")
        else:
            self.printer_lbl.config(text="未检测到打印机！请检查: 1) 打印后台服务 2) 驱动已安装", foreground='#c62828')
            self._log("未检测到打印机！")

    def _on_state(self, state, msg):
        self._log(msg)
        if state == 'approved':
            self.status_lbl.config(text='在线 - 打印机就绪', foreground='#2e7d32')
        elif state == 'warning':
            self.status_lbl.config(text='未检测到打印机', foreground='#e65100')
        elif state == 'pending':
            self.status_lbl.config(text='等待审批...', foreground='#e65100')
        elif state == 'error':
            self.status_lbl.config(text='错误 - 查看日志', foreground='#c62828')
        else:
            self.status_lbl.config(text='连接中...', foreground='#1565c0')

    def _log(self, msg):
        self.log_txt.config(state=self.tk.NORMAL)
        ts = time.strftime("%H:%M:%S")
        self.log_txt.insert(self.tk.END, f"[{ts}] {msg}\n")
        self.log_txt.see(self.tk.END)
        self.log_txt.config(state=self.tk.DISABLED)

    def _copy_token(self):
        """复制配对码到剪贴板（win32clipboard 直写 + tkinter 兜底）"""
        token = self.client.token
        ok = False
        # 方法1: win32clipboard 直接操作（最可靠）
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(token)
                ok = True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
        # 方法2: tkinter 兜底
        if not ok:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(token)
                self.root.update()  # 强制刷新剪贴板
                ok = True
            except Exception:
                pass
        if ok:
            self._log("配对码已复制！")
        else:
            self._log(f"复制失败，请手动复制: {token}")

    def _edit_template(self):
        """打开模板目录或 config.ini"""
        if TEMPLATES_DIR.exists():
            os.startfile(str(TEMPLATES_DIR))
            self._log(f"已打开模板目录: {TEMPLATES_DIR}")
        elif INI_FILE.exists():
            os.startfile(str(INI_FILE))
            self._log(f"已打开配置: {INI_FILE}")
        else:
            self._log("未找到模板目录或配置文件")

    def _close(self):
        self.root.withdraw()
        self._log("已最小化到通知区域")

    def _quit(self):
        """彻底退出"""
        _tray_del(int(self.root.frame(), 16))
        if self._hicon:
            try: _ct.windll.user32.DestroyIcon(self._hicon)
            except: pass
        self.client.stop()
        self.root.destroy()

    def _show(self):
        self.root.deiconify()
        self.root.lift()

    def _init_tray(self):
        """创建托盘图标（系统打印机图标 + 窗口过程钩子）"""
        global _tray_menu_handler, _tray_dblclick_handler
        try:
            hwnd = int(self.root.frame(), 16)
        except:
            self.root.after(1000, self._init_tray); return

        self._hicon = _get_printer_icon()

        # 设置托盘回调
        _tray_menu_handler = self._show_tray_menu
        _tray_dblclick_handler = self._show

        _tray_add(hwnd, self._hicon, "Print Relay")

        # 设置窗口图标（标题栏 + 任务栏）— 同一打印机图标
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        _ct.windll.user32.SendMessageA(hwnd, WM_SETICON, ICON_SMALL, self._hicon)
        _ct.windll.user32.SendMessageA(hwnd, WM_SETICON, ICON_BIG, self._hicon)

        self._log("托盘图标已创建")

    def _show_tray_menu(self):
        m = self.tk.Menu(self.root, tearoff=0)
        m.add_command(label="显示窗口", command=self._show)
        m.add_separator()
        m.add_command(label="退出", command=self._quit)
        try:
            m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            m.grab_release()

    def run(self):
        self.root.mainloop()

def main():
    if '--install' in sys.argv:
        _install_autostart()
        print("已注册开机启动（注册表 Run）")
        return
    if '--uninstall' in sys.argv:
        _uninstall_autostart()
        print("已移除开机启动")
        return

    # 单实例检查 — 不允许同时跑两个客户端
    _check_single_instance()

    if '--no-gui' in sys.argv:
        c = Client(); c.start()
        print(f"配对码: {c.token}"); print("按 Ctrl+C 退出")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: c.stop()
    elif '--scan' in sys.argv:
        printers = list_printers()
        if printers:
            print(f"检测到 {len(printers)} 台打印机:")
            for p in printers:
                print(f"  - {p}")
        else:
            print("未检测到打印机")
    else:
        App().run()

def _check_single_instance():
    """Windows 命名互斥体 — 防止同时跑多个客户端"""
    import ctypes as _ct2
    MUTEX_NAME = "PrintRelayClient_SingleInstance_v4"
    _ct2.windll.kernel32.CreateMutexA(None, False, MUTEX_NAME.encode())
    if _ct2.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _ct2.windll.user32.MessageBoxA(0,
            "Print Relay 已在运行中。\n请检查通知区域的图标。".encode(),
            "Print Relay".encode(), 0x40)  # MB_ICONINFORMATION
        sys.exit(0)

def _install_autostart():
    """写入注册表 HKCU\...\Run 实现开机启动 (--startup 参数隐藏窗口)"""
    import winreg
    exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
        r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, 'PrintRelay', 0, winreg.REG_SZ, f'\"{exe}\" --startup')
    winreg.CloseKey(key)

def _uninstall_autostart():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, 'PrintRelay')
        winreg.CloseKey(key)
    except: pass

if __name__ == '__main__':
    main()
