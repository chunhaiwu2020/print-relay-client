; Print Relay Client — Windows 安装包
; NSIS 3.x 脚本

!define APPNAME "Print Relay"
!define COMPANYNAME "ThreePeaks"
!define VERSION "4.4.0"
!define EXE_NAME "PrintRelay-Client.exe"

Name "${APPNAME}"
OutFile "PrintRelay-Setup-${VERSION}.exe"
InstallDir "C:\PrintRelay"
RequestExecutionLevel admin
Unicode True
SetCompressor /SOLID lzma

; ── 安装页 ──
Page directory
Page instfiles

; ── 卸载页 ──
UninstPage uninstConfirm
UninstPage instfiles

Section "Install"
    ; 主程序
    SetOutPath "$INSTDIR"
    File "dist\${EXE_NAME}"

    ; 内置模板 → C:\PrintRelay\templates\
    SetOutPath "$INSTDIR\templates"
    File "templates\*.json"

    ; ── 桌面快捷方式 ──
    CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"

    ; ── 开始菜单 ──
    CreateDirectory "$SMPROGRAMS\${APPNAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; ── 注册表开机启动 ──
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelay" '"$INSTDIR\${EXE_NAME}" --startup'

    ; ── 卸载信息 ──
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"

    ; ── 安装完毕，以当前用户身份启动（非admin） ──
    ExecShell "" "$INSTDIR\${EXE_NAME}"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\${EXE_NAME}"
    Delete "$INSTDIR\config.ini"
    RMDir /r "$INSTDIR\templates"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$DESKTOP\${APPNAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APPNAME}"

    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelay"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
