"""
Build PrintRelay-Client.exe (Windows)
用法: python build.py
依赖: pip install pywin32 pyinstaller
产物: dist/PrintRelay-Client.exe  (~15MB 单文件)
"""
import subprocess, sys

CONSOLE = '--console' in sys.argv or 'console' in sys.argv

cmd = [
    sys.executable, '-m', 'PyInstaller',
    '--onefile', '--name', 'PrintRelay-Client',
    '--collect-all', 'pywin32',
    '--collect-all', 'win32print',
    '--collect-all', 'win32api',
    '--hidden-import', 'tkinter',
    '--hidden-import', '_tkinter',
    '--clean', '--noconfirm',
]
if not CONSOLE:
    cmd.append('--windowed')
cmd.append('printer-client.py')

print(f">>> {' '.join(cmd)}")
subprocess.run(cmd, check=True)
print("\n[OK] EXE 生成在 dist/PrintRelay-Client.exe")
print("   复制到店 PC → 双击 → 复制配对码 → 面板添加配对 → 完事")
