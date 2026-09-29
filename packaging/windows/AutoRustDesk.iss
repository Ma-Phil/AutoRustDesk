; Windows 安装程序（Inno Setup 6），由 packaging/build.py 调用：
;   ISCC /DAppVersion=0.2.0 /DSourceDir=dist\AutoRustDesk /DOutputDir=dist /DIconFile=packaging\icons\autorustdesk.ico
;        [/DChsFile=简体中文界面文字.isl] AutoRustDesk.iss
; 默认装在当前用户目录下，不需要管理员权限；也可以在安装向导里选择为所有用户安装。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C7C1A0E-6F2B-4B7C-9A55-3D2E1F0A7B61}
AppName=AutoRustDesk
AppVersion={#AppVersion}
AppVerName=AutoRustDesk {#AppVersion}
AppPublisher=AutoRustDesk
AppPublisherURL=https://github.com/Ma-Phil/AutoRustDesk
AppSupportURL=https://github.com/Ma-Phil/AutoRustDesk/issues
DefaultDirName={autopf}\AutoRustDesk
DefaultGroupName=AutoRustDesk
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=AutoRustDesk-{#AppVersion}-windows-x64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\AutoRustDesk.exe
; 离线包里的 deb 已经压缩过，再压也压不小，用快速压缩
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
; 系统是中文时直接用中文，其它语言的系统先让用户选
ShowLanguageDialog=auto

[Languages]
; 先读 Default.isl 再用中文覆盖，中文里没有的条目显示英文
#ifdef ChsFile
Name: "chs"; MessagesFile: "compiler:Default.isl,{#ChsFile}"
#endif
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AutoRustDesk"; Filename: "{app}\AutoRustDesk.exe"
Name: "{autodesktop}\AutoRustDesk"; Filename: "{app}\AutoRustDesk.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoRustDesk.exe"; Description: "{cm:LaunchProgram,AutoRustDesk}"; Flags: nowait postinstall skipifsilent
