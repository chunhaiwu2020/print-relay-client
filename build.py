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
    '--icon', 'icon.ico',
    '--collect-all', 'pywin32',
    '--collect-all', 'win32print',
    '--collect-all', 'win32api',
    '--hidden-import', 'tkinter',
    '--hidden-import', '_tkinter',
    '--hidden-import', 'configparser',
    '--clean', '--noconfirm',
]
if not CONSOLE:
    cmd.append('--windowed')
cmd.append('printer-client.py')

print(f">>> {' '.join(cmd)}")
subprocess.run(cmd, check=True, env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
print("\n[OK] EXE built: dist/PrintRelay-Client.exe")
print("   Copy to store PC -> double-click to run")
