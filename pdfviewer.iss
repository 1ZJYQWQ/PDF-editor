
#define AppName "PDF 查看器"
#define AppVersion "1.0.0-pre"
#define AppExe "PDFViewer.exe"

[Setup]
AppId={{6B1D9A24-7C3E-4F58-9A61-2D8E5B4C7F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=PDF 查看器
DefaultDirName={autopf}\PDFViewer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=PDFViewer-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=packaging\app.ico

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 PDF 查看器 {#AppVersion}

WizardSelectDir=选择安装位置
WizardSelectTasks=选择附加任务
WizardReady=准备安装
WizardPreparing=正在准备
WizardInstalling=正在安装
WizardUninstalling=正在卸载

WelcomeLabel1=欢迎使用 PDF 查看器安装向导
WelcomeLabel2=此向导将引导你完成 PDF 查看器 {#AppVersion} 的安装。%n%n建议先关闭其他正在运行的程序。

SelectDirDesc=选择 PDF 查看器的安装位置。
SelectDirLabel3=将 PDF 查看器安装到以下文件夹。
SelectDirBrowseLabel=如需安装到其他位置，请单击“浏览”。

SelectTasksDesc=选择安装时同时执行的附加任务。
SelectTasksLabel2=选择要执行的附加任务，然后单击“下一步”。

ReadyLabel1=安装程序已准备就绪，即将开始安装 PDF 查看器。
ReadyLabel2a=单击“安装”开始安装；如需修改设置，请单击“上一步”。
ReadyLabel2b=单击“安装”开始安装。

PreparingDesc=正在准备安装 PDF 查看器。
InstallingLabel=正在安装，请稍候。

FinishedHeadingLabel=安装完成
FinishedLabel=PDF 查看器已安装完成，可通过桌面或开始菜单中的快捷方式启动。
FinishedLabelNoIcons=PDF 查看器已安装完成。
ClickFinish=单击“完成”退出安装程序。

ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonCancel=取消
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"
Name: "openwith"; Description: "将本程序加入“打开方式”列表（不更改当前默认程序）"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "dist\PDFViewer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\PDFViewer.pdf"; ValueType: string; ValueName: ""; ValueData: "PDF 文档"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\PDFViewer.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\PDFViewer.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: none; ValueName: "PDFViewer.pdf"; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\PDFViewer.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\PDFViewer.exe\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: openwith

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即运行 PDF 查看器"; Flags: nowait postinstall skipifsilent

[UninstallDelete]

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // ★ 静默卸载（/VERYSILENT、winget uninstall、自动更新）一律**保留数据**。
    //   实测过：把默认按钮设成「否」不够 —— /SUPPRESSMSGBOXES 会
    //   自动应答脚本里的 MsgBox，MB_DEFBUTTON2 也不生效，
    //   结果静默卸载把用户的编辑记录一并删了。
    //   只有人在那儿点、并且明确点了「是」，才删。
    if UninstallSilent then
      exit;

    DataDir := ExpandConstant('{userappdata}\PDFViewer');
    if DirExists(DataDir) then
    begin
      if MsgBox('要连编辑记录、标注和导出暂存一起删掉吗？' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                '选「否」会把它们留着，以后重装还能接着用。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
