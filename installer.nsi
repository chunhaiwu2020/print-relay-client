; Print Relay Client — Windows Installer
; NSIS 3.x + Modern UI 2 (v6 — Cloud-First)

!include "MUI2.nsh"

!define APPNAME "Print Relay"
!define COMPANYNAME "ThreePeaks"
!define VERSION "6.0.1"
!define EXE_NAME "PrintRelay-Client.exe"

Name "${APPNAME} ${VERSION}"
OutFile "SETUP.exe"
InstallDir "C:\PrintRelay"
RequestExecutionLevel admin
Unicode True
SetCompressor /SOLID lzma
BrandingText "${APPNAME} v${VERSION}"

; ── MUI Pages ──
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${APPNAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Start with Windows"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION EnableStartup
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "$INSTDIR"
    File "dist\${EXE_NAME}"

    ; ── 桌面快捷方式 ──
    CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"

    ; ── 开始菜单 ──
    CreateDirectory "$SMPROGRAMS\${APPNAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; ── 卸载信息 ──
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"
SectionEnd

Function EnableStartup
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelay" '"$INSTDIR\${EXE_NAME}" --startup'
FunctionEnd

Section "Uninstall"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelay"
    Delete "$INSTDIR\${EXE_NAME}"
    Delete "$INSTDIR\config.ini"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$DESKTOP\${APPNAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APPNAME}"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
