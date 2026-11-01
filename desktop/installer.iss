; Built on GitHub, never on the user's machine. All user data stays in %APPDATA%.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define RootPath AddBackslash(SourcePath) + "..\"

[Setup]
AppId={{C29C26B5-9705-4DB3-AFB2-6306624A0B8A}
AppName=Manipulation Radar
AppVersion={#AppVersion}
AppPublisher=ksen0v
AppPublisherURL=https://github.com/ksen0v/ExchangeAlgoDiscovery
DefaultDirName={localappdata}\Programs\ManipulationRadar
DefaultGroupName=Manipulation Radar
PrivilegesRequired=lowest
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile={#RootPath}desktop\assets\radar.ico
UninstallDisplayIcon={app}\ManipulationRadar.exe
OutputDir={#RootPath}dist
OutputBaseFilename=ManipulationRadar-Setup
Compression=lzma2/fast
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#RootPath}dist\ManipulationRadar\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Manipulation Radar"; Filename: "{app}\ManipulationRadar.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\Manipulation Radar"; Filename: "{app}\ManipulationRadar.exe"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\ManipulationRadar.exe"; Description: "Запустить Manipulation Radar"; Flags: nowait postinstall skipifsilent; Check: NormalLaunch
Filename: "{app}\ManipulationRadar.exe"; Flags: nowait; Check: UpdateLaunch

[Code]
function OpenProcess(dwDesiredAccess: LongWord; bInheritHandle: Boolean; dwProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:RADARUPDATE|0}') = '1';
end;

function NormalLaunch: Boolean;
begin
  Result := (not IsUpdate) and (ExpandConstant('{param:NORUN|0}') <> '1');
end;

function UpdateLaunch: Boolean;
begin
  Result := IsUpdate and (ExpandConstant('{param:NORUN|0}') <> '1');
end;

function InitializeSetup: Boolean;
var
  ProcessId: Integer;
  Handle: THandle;
  WaitResult: LongWord;
begin
  Result := True;
  if not IsUpdate then Exit;
  ProcessId := StrToIntDef(ExpandConstant('{param:RADARPID|0}'), 0);
  if ProcessId <= 0 then Exit;
  Handle := OpenProcess($00100000, False, ProcessId); { SYNCHRONIZE only }
  if Handle = 0 then Exit; { the old process may already have exited }
  try
    WaitResult := WaitForSingleObject(Handle, 60000);
    if WaitResult <> 0 then begin
      MsgBox('Приложение ещё не завершило работу. Закройте Manipulation Radar и повторите установку.', mbError, MB_OK);
      Result := False;
    end;
  finally
    CloseHandle(Handle);
  end;
end;
