; Print Relay Client - Windows 7 Legacy Installer

!include "MUI2.nsh"

!define APPNAME "Print Relay Win7"
!define COMPANYNAME "ThreePeaks"
!define VERSION "6.0.4"
!define EXE_NAME "PrintRelay-Client-Win7.exe"

Name "${APPNAME} ${VERSION}"
OutFile "SETUP-Win7-${VERSION}.exe"
InstallDir "C:\PrintRelay-Win7"
RequestExecutionLevel admin
Unicode True
SetCompressor /SOLID lzma
BrandingText "${APPNAME} v${VERSION}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "$INSTDIR"
    File "dist\${EXE_NAME}"

    CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"

    CreateDirectory "$SMPROGRAMS\${APPNAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}"
    CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"

    ClearErrors
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelayWin7" '"$INSTDIR\${EXE_NAME}" --startup'
    IfErrors 0 +3
        MessageBox MB_ICONEXCLAMATION "Startup registry write failed. Please run installer as administrator."
        Goto +6
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelayWin7"
    StrCmp $0 '"$INSTDIR\${EXE_NAME}" --startup' +3 0
        MessageBox MB_ICONEXCLAMATION "Startup registry verification failed."
        Goto +2
    DetailPrint "Startup registered: $0"

    ExecShell "" "$INSTDIR\${EXE_NAME}"
SectionEnd

Section "Uninstall"
    DeleteRegValue HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelayWin7"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PrintRelayWin7"
    Delete "$INSTDIR\${EXE_NAME}"
    Delete "$INSTDIR\config.ini"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$DESKTOP\${APPNAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APPNAME}"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
