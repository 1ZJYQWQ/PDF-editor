; PDF 查看器 —— 后端（非 Electron）版的 Windows 安装包
;
; 为什么用 NSIS 而不是 Inno Setup：
;   本机没有 ISCC，而 electron-builder 的缓存里带了 makensis 3.0.4.1，
;   能直接编。同一个仓库里两个安装包（后端版 / Electron 版）都能自足地编出来，
;   不依赖任何额外安装的软件。pdfviewer.iss 仍然保留，装了 Inno 的话也能编。
;
; 编译（在项目根目录）：
;   "%LOCALAPPDATA%\electron-builder\Cache\nsis\nsis-3.0.4.1\Bin\makensis.exe" ^
;       -DVERSION=1.2.0-pre -DVERNUM=1.2.0.0 packaging\installer.nsi
;   （输入目录固定取 ..\dist\PDFViewer，产物固定落到 ..\dist\）
;
;   脚本必须以 UTF-8 BOM 保存：makensis 默认按系统 ACP 读脚本，
;   没有 BOM 会把中文界面文案读成乱码，然后直接报 "Bad text encoding"。
;
; 行为约定（与 pdfviewer.iss 一致）：
;   · **只装给当前用户**（%LOCALAPPDATA%\Programs），全程不弹 UAC；
;   · .pdf 只登记到「打开方式」，不改默认打开程序；
;   · 卸载**默认保留** %APPDATA%\PDFViewer 里的标注/编辑数据，
;     静默卸载（/S）一律不删 —— 数据比程序重要；
;   · 静默安装：setup.exe /S

Unicode true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
; ★ 路径一律写成**相对脚本目录**的形式，不从命令行传绝对路径。
;   项目路径里有中文（"项目"），从 Git Bash 传进来会被当成 ACP 字节
;   交给 makensis，编出来是乱码路径，File 直接失败。
;   另外别用 ${__FILEDIR__} 拼：它返回的是"脚本所在的相对目录"，
;   再拼一次会得到 packaging\packaging\app.ico。
!ifndef APP_SRC
  !define APP_SRC "..\dist\PDFViewer"
!endif
; exe 的版本资源只能是四段数字，"1.1.5-pre" 这种写法编不过
!ifndef VERNUM
  !define VERNUM "0.0.0.0"
!endif

!define APP_NAME "PDF 查看器（浏览器版）"
!define APP_EXE "PDFViewer.exe"
!define APP_KEY "Software\PDFViewer-Browser"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\PDFViewer-Browser"
!define PROGID "PDFViewerBrowser.pdf"

Name "${APP_NAME} ${VERSION}"
OutFile "..\dist\PDFViewer-${VERSION}-setup.exe"
; ★ 目录必须和 Electron 版分开。electron-builder 装到
;   %LOCALAPPDATA%\Programs\PDFViewer，两个版本混在一个目录里
;   会互相覆盖文件，卸载任何一个都会把另一个弄坏。
InstallDir "$LOCALAPPDATA\Programs\PDFViewer-Browser"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
; 只写当前用户 → 不触发 UAC。装到 Program Files 才需要管理员，
; 而这个程序定位是"自己电脑上的小工具"，免 UAC 比装到公共目录更值。
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
BrandingText "${APP_NAME} ${VERSION}"
ShowInstDetails hide
ShowUninstDetails hide
AutoCloseWindow false

VIProductVersion "${VERNUM}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

!define MUI_ICON "app.ico"
!define MUI_UNICON "app.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------- 安装
Section "主程序" SecMain
  SetOutPath "$INSTDIR"
  ; 整个目录版一起装：exe 脱离 _internal 跑不起来
  File /r "${APP_SRC}\*.*"

  WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${APP_KEY}" "Version" "${VERSION}"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 控制面板「应用和功能」里的条目
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

; ---------------------------------------------------------------- 打开方式
; ★ 只登记到 OpenWithProgids —— 不改用户当前的默认 PDF 阅读器。
;   抢默认打开程序是很讨嫌的行为，装个工具不该顺手改掉别人的习惯。
Section "登记到「打开方式」" SecAssoc
  WriteRegStr HKCU "Software\Classes\${PROGID}" "" "PDF 文档"
  WriteRegStr HKCU "Software\Classes\${PROGID}\DefaultIcon" "" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKCU "Software\Classes\${PROGID}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
  WriteRegStr HKCU "Software\Classes\.pdf\OpenWithProgids" "${PROGID}" ""
  WriteRegStr HKCU "Software\Classes\Applications\${APP_EXE}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
SectionEnd

; ---------------------------------------------------------------- 卸载
Section "Uninstall"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  ; 先删程序文件；uninstall.exe 自己删不掉，留到最后 RMDir /r
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\*.dll"
  Delete "$INSTDIR\*.pyd"
  Delete "$INSTDIR\*.txt"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${APP_KEY}"
  DeleteRegKey HKCU "Software\Classes\${PROGID}"
  DeleteRegValue HKCU "Software\Classes\.pdf\OpenWithProgids" "${PROGID}"
  DeleteRegKey HKCU "Software\Classes\Applications\${APP_EXE}"

  ; ★ 数据目录：%APPDATA%\PDFViewer 里是用户的标注与编辑数据。
  ;   静默卸载（/S）一律不删 —— 没有人盯着的时候，删数据是最后悔的操作。
  ;   有界面时才问一句，默认按钮是「保留」。
  IfSilent keepData
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "要一并删除标注和编辑数据吗？$\r$\n（位于 %APPDATA%\PDFViewer）$\r$\n$\r$\n选「否」只会删掉程序，数据留着，以后重装还能接着用。" \
    IDNO keepData
  RMDir /r "$APPDATA\PDFViewer"
  keepData:

  RMDir /r "$INSTDIR"
SectionEnd

Function .onInit
  ; 静默安装时不显示任何界面
  ${If} ${Silent}
    SetAutoClose true
  ${EndIf}
FunctionEnd

Function un.onInit
  ${If} ${Silent}
    SetAutoClose true
  ${EndIf}
FunctionEnd
