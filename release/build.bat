@echo off
chcp 65001 >nul
title PrintRelay 一键打包

echo ========================================
echo   Print Relay Client — 一键打包
echo   Restaurant Asia Shanghai
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 需要 Python 3.8+
    echo 请先从 https://python.org 下载安装
    echo （安装时勾选 "Add Python to PATH"）
    pause
    exit /b 1
)
echo [OK] Python 已就绪

:: 自动装依赖
echo.
echo [*] 安装依赖...
pip install pywin32 pyinstaller -q
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败，请检查网络
    pause
    exit /b 1
)
echo [OK] 依赖已就绪

:: 打包
echo.
echo [*] 开始打包... (约 1-2 分钟)
python build.py
if %errorlevel% neq 0 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   [OK] 完成！
echo.
echo   EXE 位置: dist\PrintRelay-Client.exe
echo   复制到店 PC - 双击运行
echo ========================================
pause
