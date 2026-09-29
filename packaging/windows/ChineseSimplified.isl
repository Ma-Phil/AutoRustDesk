; AutoRustDesk 安装向导的简体中文界面文字（Inno Setup 6）。
; 只翻译了安装、卸载过程中看得到的文字：AutoRustDesk.iss 先读 Default.isl，再用这里的内容覆盖，
; 没有翻译的条目（很少出现的提示）显示英文。
; Inno Setup 本身带有简体中文翻译时，packaging/build.py 优先用它带的。

[LangOptions]
LanguageName=<7B80><4F53><4E2D><6587>
LanguageID=$0804
LanguageCodePage=936

[Messages]
SetupAppTitle=安装
SetupWindowTitle=安装 - %1
UninstallAppTitle=卸载
UninstallAppFullTitle=卸载 %1

InformationTitle=信息
ConfirmTitle=确认
ErrorTitle=错误

SetupLdrStartupMessage=现在将安装 %1。要继续吗？
ExitSetupTitle=退出安装
ExitSetupMessage=安装还没有完成。现在退出的话，程序不会被安装。%n%n以后可以再运行安装程序完成安装。%n%n确定要退出吗？

ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonOK=确定
ButtonCancel=取消
ButtonYes=是(&Y)
ButtonYesToAll=全部是(&A)
ButtonNo=否(&N)
ButtonNoToAll=全部否(&O)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)...
ButtonWizardBrowse=浏览(&R)...
ButtonNewFolder=新建文件夹(&M)

SelectLanguageTitle=选择安装语言
SelectLanguageLabel=选择安装时使用的语言。

BrowseDialogTitle=浏览文件夹
BrowseDialogLabel=在下面的列表中选择一个文件夹，然后点"确定"。
NewFolderName=新建文件夹

WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=将在这台电脑上安装 [name/ver]。%n%n建议先关闭其它正在运行的程序，再继续安装。

PrivilegesRequiredOverrideTitle=选择安装方式
PrivilegesRequiredOverrideInstruction=选择安装方式
PrivilegesRequiredOverrideText1=%1 可以为所有用户安装（需要管理员权限），也可以只为你自己安装。
PrivilegesRequiredOverrideText2=%1 可以只为你自己安装，也可以为所有用户安装（需要管理员权限）。
PrivilegesRequiredOverrideAllUsers=为所有用户安装(&A)
PrivilegesRequiredOverrideAllUsersRecommended=为所有用户安装(&A)（推荐）
PrivilegesRequiredOverrideCurrentUser=只为我安装(&M)
PrivilegesRequiredOverrideCurrentUserRecommended=只为我安装(&M)（推荐）

WizardSelectDir=选择安装位置
SelectDirDesc=要把 [name] 安装到哪里？
SelectDirLabel3=将把 [name] 安装到下面的文件夹。
SelectDirBrowseLabel=点"下一步"继续。要换一个文件夹，请点"浏览"。
DiskSpaceGBLabel=至少需要 [gb] GB 可用磁盘空间。
DiskSpaceMBLabel=至少需要 [mb] MB 可用磁盘空间。

WizardSelectTasks=选择附加任务
SelectTasksDesc=还需要做哪些事？
SelectTasksLabel2=选择安装 [name] 时要一起做的事，然后点"下一步"。

WizardReady=准备安装
ReadyLabel1=已经准备好在这台电脑上安装 [name]。
ReadyLabel2a=点"安装"开始安装。要查看或修改设置，请点"上一步"。
ReadyLabel2b=点"安装"开始安装。
ReadyMemoDir=安装位置：
ReadyMemoTasks=附加任务：

WizardPreparing=正在准备安装
PreparingDesc=正在准备在这台电脑上安装 [name]。

WizardInstalling=正在安装
InstallingLabel=正在这台电脑上安装 [name]，请稍候。

FinishedHeadingLabel=[name] 安装完成
FinishedLabelNoIcons=已经在这台电脑上装好了 [name]。
FinishedLabel=已经在这台电脑上装好了 [name]，可以从开始菜单或桌面快捷方式打开。
ClickFinish=点"完成"退出安装程序。

StatusCreateDirs=正在创建文件夹...
StatusExtractFiles=正在解压文件...
StatusCreateIcons=正在创建快捷方式...
StatusSavingUninstall=正在保存卸载信息...
StatusRunProgram=正在完成安装...
StatusRollback=正在撤销更改...

WizardUninstalling=卸载进度
StatusUninstalling=正在卸载 %1...
ConfirmUninstall=确定要删除 %1 及其全部组件吗？
UninstallStatusLabel=正在从这台电脑上删除 %1，请稍候。
UninstalledAll=已经从这台电脑上删除了 %1。

[CustomMessages]
CreateDesktopIcon=创建桌面快捷方式(&D)
AdditionalIcons=快捷方式：
LaunchProgram=运行 %1
UninstallProgram=卸载 %1
ProgramOnTheWeb=%1 网站
