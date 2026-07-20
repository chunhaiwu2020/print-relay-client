"""
Build PrintRelay-Client-Win7.exe.
Use Python 3.8.10 for Windows 7 compatibility.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = '--console' in sys.argv or 'console' in sys.argv

cmd = [
    sys.executable, '-m', 'PyInstaller',
    '--onefile', '--name', 'PrintRelay-Client-Win7',
    '--add-data', f'{os.path.join(HERE, "logo.ico")};.',
    '--icon', os.path.join(HERE, 'logo.ico'),
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
subprocess.run(cmd, check=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
print("\n[OK] EXE built: dist/PrintRelay-Client-Win7.exe")
