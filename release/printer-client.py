#!/usr/bin/env python3
"""
Print Relay Client v3 — 客户端渲染版
启动 → 显示配对码 → 扫描打印机 → 管理员在面板添加 → 自动连接
服务器发 JSON → 客户端 Jinja2 渲染 ESC/POS → 打印机
"""

import socket, struct, json, os, sys, time, threading, secrets, logging
from datetime import datetime
from pathlib import Path
from jinja2 import Template

# ── 硬编码 ───────────────────────────────────────────────────
RELAY_HOST = "relay.thecarte.eu"
RELAY_PORT = 51900
PAPER_WIDTH = 80
TEMPLATE_FILE = (Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / "ticket.j2")  # 默认模板

# ── 配置 ─────────────────────────────────────────────────────
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

def render_ticket(template_str, order, width=80):
    """Jinja2 模板 → ESC/POS 字节"""
    template = Template(template_str)
    text = template.render(order=order, now=datetime.now())
    out = escpos_init()
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line: continue
        align, bold = 'left', False
        parts = line.split(' ', 1)
        while parts and parts[0].startswith('.'):
            cmd = parts[0]
            if cmd == '.center': align = 'center'
            elif cmd == '.right': align = 'right'
            elif cmd == '.bold': bold = True
            elif cmd.startswith('.div'):
                char = cmd[4:] if len(cmd) > 4 else '-'
                out += escpos_div(char, width); parts = []; break
            elif cmd == '.cut':
                out += escpos_cut(); parts = []; break
            elif cmd == '.item':
                rest = parts[1] if len(parts) > 1 else ''
                toks = rest.split(' ', 2)
                qty, name, prc = (toks + ['', '', ''])[:3]
                lt = int(qty or 1) * float(prc or 0)
                left = f"{qty}x  {name}"[:30]
                right = f"€{lt:,.2f}".replace('.', ',')
                n = 48 if width >= 80 else 32
                pad = n - len(left) - len(right)
                out += (left + ' ' * max(1, pad) + right).encode('latin-1', errors='replace') + b'\n'
                parts = []; break
            parts = parts[1].split(' ', 1) if len(parts) > 1 else []
        if parts and parts[0]:
            out += escpos_align(align)
            if bold: out += escpos_bold(True)
            out += escpos_line(' '.join(parts) if isinstance(parts, list) else line, width)
            if bold: out += escpos_bold(False)
    return out


# ── 客户端核心 ───────────────────────────────────────────────
class Client:
    def __init__(self):
        self.token, _ = load_or_create_token()
        if not self.token:
            self.token = secrets.token_hex(4)  # 8 位 hex
            save_token(self.token)
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

                    # 加载模板并渲染
                    try:
                        template_str = TEMPLATE_FILE.read_text(encoding='utf-8')
                    except:
                        log.error(f"无法加载模板: {TEMPLATE_FILE}")
                        continue

                    ticket = render_ticket(template_str, order, PAPER_WIDTH)

                    printer = target_printer or (self.printers[0] if self.printers else None)
                    if printer:
                        send_raw(printer, ticket)
                        onum = order.get('number', '?')
                        self._state('approved', f'打印 #{onum} ({len(ticket)}B) -> {printer}')
                    else:
                        log.warning("无可用打印机")
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

    def _close(self):
        self.client.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

def main():
    if '--no-gui' in sys.argv:
        c = Client(); c.start()
        print(f"配对码: {c.token}"); print("按 Ctrl+C 退出")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: c.stop()
    elif '--scan' in sys.argv:
        # 命令行模式：仅扫描打印机（诊断用）
        printers = list_printers()
        if printers:
            print(f"检测到 {len(printers)} 台打印机:")
            for p in printers:
                print(f"  - {p}")
        else:
            print("未检测到打印机")
            print("请检查: 1) Print Spooler 服务是否运行")
            print("        2) 是否安装了打印机驱动")
    else:
        App().run()

if __name__ == '__main__':
    main()
