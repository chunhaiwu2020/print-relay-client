#!/usr/bin/env python3
"""
Print Relay Client v6 — Cloud-First 纯通道
启动 → 扫描打印机 → 面板配对 → TCP 长连
relay 发送 ticket_b64 → 客户端 base64 解码 → send_raw() 吐出
零本地渲染，零模板文件，纯管道。
"""

import socket, struct, json, os, sys, time, threading, secrets, logging, configparser, base64
from datetime import datetime
from pathlib import Path

# ── 硬编码 ───────────────────────────────────────────────────
RELAY_HOST = "printrelay.es"
RELAY_PORT = 51902
PAPER_WIDTH = 80

# 配置文件均在 EXE 目录下
_EXE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
INI_FILE = _EXE_DIR / "config.ini"

def load_config():
    """读取 config.ini"""
    cfg = configparser.ConfigParser()
    if INI_FILE.exists():
        cfg.read(INI_FILE, encoding='utf-8')
        log.info(f"Config loaded: {len(cfg.sections())} printer sections")
    else:
        log.warning(f"Config not found: {INI_FILE} — place config.ini next to EXE")
    return cfg

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
            log.warning(f"EnumPrinters level 1 failed: {e}")

        # Method 2: local (level 2, incl virtual)
        try:
            add(p['pPrinterName'] for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL, None, 2) if p.get('pPrinterName'))
        except Exception as e:
            log.warning(f"EnumPrinters level 2 failed: {e}")

        # Method 3: default printer fallback
        try:
            def_printer = win32print.GetDefaultPrinter()
            if def_printer:
                add([def_printer])
        except Exception as e:
            log.warning(f"GetDefaultPrinter failed: {e}")

        printers = sorted(seen)  # sorted, stable order
        if printers:
            log.info(f"Found {len(printers)} printers: {printers}")
        else:
            log.warning("No printers detected! Check: 1) Print Spooler running 2) Printer drivers")
        return printers
    except ImportError:
        log.error("pywin32 not installed, cannot enumerate printers")
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
        log.error(f"Print failed: {e}"); return False


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
        """Rescan printers, notify server if connected"""
        self.printers = list_printers()
        if self.printers:
            log.info(f"Scan complete: {self.printers}")
        else:
            log.warning("Scan complete: no printers found")
        # If connected, notify server
        with self._sock_lock:
            if self._sock:
                try:
                    info = json.dumps({"paper_width": PAPER_WIDTH, "printers": self.printers})
                    self._sock.sendall(info.encode() + b"\n")
                    log.info("Server notified of printer list update")
                except Exception as e:
                    log.warning(f"Failed to notify server: {e}")
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
            except Exception as e: log.error(f"Loop error: {e}")
            if self.running:
                self._state('connecting', 'Reconnecting in 5s...')
                for _ in range(5):
                    if not self.running: return
                    time.sleep(1)

    def _run(self):
        self._state('connecting', f'Connecting to {RELAY_HOST}:{RELAY_PORT}...')

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((RELAY_HOST, RELAY_PORT))

        # TCP keepalive — prevent NAT timeout disconnect
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except: pass

        # REGISTER
        sock.sendall(f"REGISTER {self.token}\n".encode())

        # Send printer list
        if not self.printers:
            self.printers = list_printers()
        if not self.printers:
            self._state('warning', 'No printers found! Check print spooler')
        info = json.dumps({"paper_width": PAPER_WIDTH, "printers": self.printers})
        sock.sendall(info.encode() + b"\n")

        # Wait for server response
        resp = sock.recv(1024).strip()
        if resp == b'UNKNOWN_TOKEN':
            self._state('error', 'Unknown token — add this code in control panel')
            sock.close(); return
        if resp == b'WRONG_TYPE':
            self._state('error', 'Token is not a client type')
            sock.close(); return

        if resp == b'PENDING':
            self._state('pending', f'Waiting approval... Code: {self.token}')
            while self.running:
                try:
                    r2 = sock.recv(1024).strip()
                    if r2 == b'APPROVED':
                        self._state('approved', 'Approved! Printer ready.')
                        break
                except: break
        elif resp == b'APPROVED':
            self._state('approved', 'Connected & approved')
        else:
            self._state('error', f'Unknown response: {resp}')
            sock.close(); return

        # Normal mode — receive print jobs
        with self._sock_lock:
            self._sock = sock
        sock.settimeout(60)
        printer_str = ', '.join(self.printers) if self.printers else 'None'
        self._state('approved', f'Online | Printers: {printer_str}')
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
                        log.warning(f"Invalid JSON: {data[:100]}")
                        continue

                    ticket_b64 = msg.get('ticket_b64', '')
                    pdf_b64 = msg.get('pdf_b64', '')
                    target_printer = msg.get('printer', '')

                    if pdf_b64:
                        import tempfile, os as _os
                        pdf_data = base64.b64decode(pdf_b64)
                        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
                            f.write(pdf_data)
                            pdf_path = f.name
                        try:
                            import win32api
                            win32api.ShellExecute(0, "print", pdf_path, None, ".", 0)
                            ok = True
                        except Exception as e:
                            log.error(f'PDF print failed: {e}')
                            ok = False
                        # Cleanup after 10s
                        def _clean():
                            time.sleep(10)
                            try: _os.unlink(pdf_path)
                            except: pass
                        threading.Thread(target=_clean, daemon=True).start()
                        onum = msg.get('order_id', '?')
                        if ok:
                            self._state('approved', f'📄 #{onum} ({len(pdf_data)}B PDF) -> {target_printer}')
                        else:
                            self._state('error', f'PDF print failed #{onum} -> {target_printer}')
                        continue

                    if not ticket_b64 or not target_printer:
                        continue

                    ticket = base64.b64decode(ticket_b64)
                    printer = target_printer
                    ok = send_raw(printer, ticket)
                    onum = msg.get('order_id', '?')
                    if ok:
                        self._state('approved', f'☁️ #{onum} ({len(ticket)}B) -> {printer}')
                    else:
                        self._state('error', f'Print failed #{onum} -> {printer}')
                except socket.timeout:
                    continue
                except:
                    break
        finally:
            with self._sock_lock:
                self._sock = None
            try: sock.close()
            except: pass
            self._state('connecting', 'Disconnected')

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
# 托盘图标 (ctypes, 窗口过程钩子 — 不轮询)
import ctypes as _ct
from ctypes import wintypes as _w

_NIM_ADD, _NIM_DELETE = 0, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 1, 2, 4
_WM_TRAY = 0x8000 + 1

class _NOTIFYICONDATA(_ct.Structure):
    _fields_ = [("cbSize", _w.DWORD), ("hWnd", _w.HWND), ("uID", _w.UINT),
                ("uFlags", _w.UINT), ("uCallbackMessage", _w.UINT),
                ("hIcon", _w.HICON), ("szTip", _w.CHAR * 128)]

def _get_printer_icon():
    """加载自定义图标 (从嵌入的 logo.ico)"""
    import os
    # PyInstaller frozen: _MEIPASS, 否则脚本目录
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(base, 'logo.ico')
    try:
        # LR_LOADFROMFILE=0x10, LR_DEFAULTSIZE=0x40, IMAGE_ICON=1
        hicon = _ct.windll.user32.LoadImageW(0, ico_path, 1, 0, 0, 0x10 | 0x40)
        if hicon:
            return hicon
    except Exception:
        pass
    # Fallback: Windows 默认应用图标
    return _ct.windll.user32.LoadIconW(0, 32512)

# 窗口过程钩子回调
_WNDPROC = _ct.WINFUNCTYPE(_ct.c_void_p, _w.HWND, _ct.c_uint, _w.WPARAM, _w.LPARAM)
_orig_proc = None
_tray_cb = {}

def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_TRAY:
        if lparam == 0x0205:  # 右键
            if 'menu' in _tray_cb: _tray_cb['menu']()
        elif lparam in (0x0202, 0x0203):  # 单击/双击
            if 'show' in _tray_cb: _tray_cb['show']()
        return 0
    return _ct.windll.user32.CallWindowProcA(_orig_proc, hwnd, msg, wparam, lparam)

class App:
    def __init__(self):
        import tkinter as tk; from tkinter import ttk
        self.tk = tk; self.ttk = ttk
        self.client = Client()
        self.client.on_state = lambda s, m: self.root.after(0, self._on_state, s, m)

        self.root = tk.Tk()
        self.root.title(f"Print Relay - {CLIENT_NAME}")
        self.root.geometry("460x540")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        # 窗口图标 (exe图标 / 任务栏)
        _ico = os.path.join(
            sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__)),
            'logo.ico')
        if os.path.exists(_ico):
            self.root.iconbitmap(default=_ico)

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

        # Title
        f = tt.Frame(self.root, padding=16)
        f.pack(fill=t.X)
        tt.Label(f, text="Print Relay Client", font=('Segoe UI', 14, 'bold')).pack()
        tt.Label(f, text=f"PC: {CLIENT_NAME}", foreground='#666').pack()

        tt.Separator(self.root, orient=t.HORIZONTAL).pack(fill=t.X, padx=16)

        # Status
        sf = tt.Frame(self.root, padding=12)
        sf.pack(fill=t.X)
        self.status_lbl = tt.Label(sf, text="Starting...", font=('Segoe UI', 11))
        self.status_lbl.pack()

        # Pairing Code
        tf = tt.LabelFrame(self.root, text="Pairing Code", padding=14)
        tf.pack(fill=t.X, padx=16, pady=(8,4))

        tok_row = tt.Frame(tf)
        tok_row.pack(fill=t.X)
        self.token_lbl = tt.Label(tok_row, text=self.client.token, font=('Consolas', 22, 'bold'), foreground='#1565c0')
        self.token_lbl.pack(side=t.LEFT, padx=(0,12))
        self.copy_btn = tt.Button(tok_row, text="Copy Code", command=self._copy_token)
        self.copy_btn.pack(side=t.LEFT)
        tt.Label(tf, text="Enter this code in the control panel to pair", foreground='#999', font=('Segoe UI', 9)).pack(pady=(8,0))

        # Printer Configuration
        pf = tt.LabelFrame(self.root, text="Printer Configuration", padding=12)
        pf.pack(fill=t.X, padx=16, pady=(8,4))

        self.stations = {}      # {key: {'var': tk.IntVar, 'combo': ttk.Combobox}}
        self.printers_list = []  # 缓存扫描结果

        for key, label in [('1', 'Station 1'), ('2', 'Station 2'), ('3', 'Station 3')]:
            row = tt.Frame(pf)
            row.pack(fill=t.X, pady=(0,3))
            v = t.IntVar(value=1 if key == '1' else 0)
            cb = tt.Checkbutton(row, text=label, variable=v,
                                command=lambda k=key: self._on_check_changed(k))
            cb.pack(side=t.LEFT)
            combo = tt.Combobox(row, state='readonly', width=28)
            combo.pack(side=t.LEFT, padx=(8,0))
            self.stations[key] = {'var': v, 'combo': combo}

        btn_row = tt.Frame(pf)
        btn_row.pack(fill=t.X, pady=(8,0))
        self.save_btn = tt.Button(btn_row, text="💾 Save Config", command=self._save_config)
        self.save_btn.pack(side=t.LEFT)
        self.scan_btn = tt.Button(btn_row, text="🔄 Rescan", command=self._scan_and_show)
        self.scan_btn.pack(side=t.LEFT, padx=(8,0))

        # Load existing config.ini
        self._load_existing_config()

        # Log
        lf = tt.LabelFrame(self.root, text="Log", padding=8)
        lf.pack(fill=t.BOTH, expand=True, padx=16, pady=(4,12))
        self.log_txt = t.Text(lf, height=5, wrap=t.WORD, font=('Consolas', 8), state=t.DISABLED)
        self.log_txt.pack(fill=t.BOTH, expand=True)

    def _scan_and_show(self):
        """扫描打印机并更新下拉"""
        self._log("Scanning printers...")
        self.scan_btn.config(state='disabled', text="Scanning...")
        def do_scan():
            printers = self.client.scan_printers()
            self.root.after(0, lambda: self._on_scan_done(printers))
        threading.Thread(target=do_scan, daemon=True).start()

    def _on_scan_done(self, printers):
        self.scan_btn.config(state='normal', text="🔄 Rescan")
        self.printers_list = printers
        if printers:
            self._log(f"Found {len(printers)} printers: {', '.join(printers)}")
            self._refresh_dropdowns(printers)
        else:
            self._log("No printers found! Check spooler & drivers")

    def _refresh_dropdowns(self, printers):
        """刷新所有勾选站点的打印机下拉"""
        opts = printers or self.printers_list or []
        default = opts[0] if opts else ''
        for key, st in self.stations.items():
            combo = st['combo']
            combo['values'] = opts
            cur = combo.get()
            if not cur or cur not in opts:
                combo.set(default if default else '')

    def _on_check_changed(self, key):
        """勾选/取消勾选时切换下拉状态"""
        st = self.stations[key]
        combo = st['combo']
        if st['var'].get():
            combo.config(state='readonly')
            if not combo.get() and self.printers_list:
                combo.set(self.printers_list[0])
        else:
            combo.config(state='disabled')
            combo.set('')

    def _load_existing_config(self):
        """Load existing config.ini presets"""
        cfg = self.client.config
        if not cfg.sections():
            return
        for key, label in [('1', 'Station 1'), ('2', 'Station 2'), ('3', 'Station 3')]:
            sec = f'printer.{key}'
            if cfg.has_section(sec):
                name = cfg.get(sec, 'name', fallback='')
                st = self.stations[key]
                st['var'].set(1)
                if name:
                    st['combo'].set(name)
                    st['combo'].config(state='readonly')

    def _save_config(self):
        """Generate config.ini (printer name mapping only)"""
        from configparser import ConfigParser

        cfg = ConfigParser()
        cfg.optionxform = str
        built = []

        for key, label in [('1', 'Station 1'), ('2', 'Station 2'), ('3', 'Station 3')]:
            st = self.stations[key]
            if not st['var'].get():
                continue
            printer = st['combo'].get()
            if not printer:
                self._log(f"✗ {label}: no printer selected, skipped")
                continue
            sec = f'printer.{key}'
            cfg.add_section(sec)
            cfg.set(sec, 'name', printer)
            built.append(label)

        if not built:
            self._log("✗ No station enabled")
            return

        with open(INI_FILE, 'w', encoding='utf-8') as f:
            cfg.write(f)

        self.client.config = load_config()
        self._log(f"✓ Config saved: {', '.join(built)}")

    def _on_state(self, state, msg):
        self._log(msg)
        if state == 'approved':
            self.status_lbl.config(text='Online - Printer ready', foreground='#2e7d32')
        elif state == 'warning':
            self.status_lbl.config(text='No printer detected', foreground='#e65100')
        elif state == 'pending':
            self.status_lbl.config(text='Waiting approval...', foreground='#e65100')
        elif state == 'error':
            self.status_lbl.config(text='Error - check log', foreground='#c62828')
        else:
            self.status_lbl.config(text='Connecting...', foreground='#1565c0')

    def _log(self, msg):
        self.log_txt.config(state=self.tk.NORMAL)
        ts = time.strftime("%H:%M:%S")
        self.log_txt.insert(self.tk.END, f"[{ts}] {msg}\n")
        self.log_txt.see(self.tk.END)
        self.log_txt.config(state=self.tk.DISABLED)

    def _copy_token(self):
        """Copy token to clipboard (win32clipboard + tkinter fallback)"""
        token = self.client.token
        ok = False
        # Method 1: win32clipboard direct (most reliable)
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
        # Method 2: tkinter fallback
        if not ok:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(token)
                self.root.update()  # Force clipboard flush
                ok = True
            except Exception:
                pass
        if ok:
            self._log("Code copied!")
        else:
            self._log(f"Copy failed, copy manually: {token}")

    def _close(self):
        """Minimize to tray"""
        self.root.withdraw()
        self._log("Minimized to tray")

    def _quit(self):
        """Full quit"""
        try:
            _ct.windll.shell32.Shell_NotifyIconA(_NIM_DELETE, _ct.byref(_NOTIFYICONDATA()))
        except: pass
        if self._hicon:
            try: _ct.windll.user32.DestroyIcon(self._hicon)
            except: pass
        self.client.stop()
        self.root.destroy()

    def _show(self):
        """还原窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _init_tray(self):
        """创建托盘图标 (窗口过程钩子)"""
        global _orig_proc
        try:
            hwnd = int(self.root.frame(), 16)
        except:
            self.root.after(1000, self._init_tray); return

        hicon = _get_printer_icon()
        self._hicon = hicon

        # 安装窗口过程钩子
        _orig_proc = _ct.windll.user32.SetWindowLongPtrA(
            hwnd, -4, _ct.cast(_WNDPROC(_wnd_proc), _ct.c_void_p).value)

        # 设置回调
        _tray_cb['show'] = self._show
        _tray_cb['menu'] = self._show_tray_menu

        # 添加托盘图标
        nid = _NOTIFYICONDATA()
        nid.cbSize = _ct.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAY
        nid.hIcon = hicon
        nid.szTip = b"Print Relay"
        _ct.windll.shell32.Shell_NotifyIconA(_NIM_ADD, _ct.byref(nid))
        self._log("Tray icon created")

    def _show_tray_menu(self):
        m = self.tk.Menu(self.root, tearoff=0)
        m.add_command(label="Show Window", command=self._show)
        m.add_separator()
        m.add_command(label="Exit", command=self._quit)
        try:
            m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            m.grab_release()

    def run(self):
        self.root.mainloop()

def main():
    if '--install' in sys.argv:
        _install_autostart()
        print("Startup registered")
        return
    if '--uninstall' in sys.argv:
        _uninstall_autostart()
        print("Startup removed")
        return

    # 单实例检查
    _check_single_instance()

    if '--no-gui' in sys.argv:
        c = Client(); c.start()
        print(f"Pairing code: {c.token}"); print("Press Ctrl+C to exit")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: c.stop()
    elif '--scan' in sys.argv:
        printers = list_printers()
        if printers:
            print(f"Found {len(printers)} printers:")
            for p in printers: print(f"  - {p}")
        else:
            print("No printers found")
    else:
        App().run()

def _check_single_instance():
    """Click shortcut → kill old sleeping thread → start fresh"""
    import subprocess, os as _os
    exe_name = _os.path.basename(sys.executable) if getattr(sys, 'frozen', False) else _os.path.basename(sys.argv[0])

    # Kill any existing instances of this EXE (except self)
    subprocess.run(
        f'taskkill /f /im "{exe_name}" /fi "PID ne {_os.getpid()}"',
        shell=True, capture_output=True, timeout=5
    )
    time.sleep(0.5)  # Let Windows clean up old process handles

def _install_autostart():
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
