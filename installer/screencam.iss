; Instalador do ScreenCam ONVIF (Inno Setup 6)
; Compilar com: ISCC.exe installer\screencam.iss  (o build.ps1 faz isso)

#define AppName "ScreenCam ONVIF"
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppExe "ScreenCamONVIF.exe"
#define DataDir "{commonappdata}\ScreenCamONVIF"

[Setup]
AppId={{B4C1E0F2-5A7D-4C8E-9F3A-2D6B8E1C4A90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ScreenCam
DefaultDirName={autopf}\ScreenCamONVIF
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ScreenCamONVIF-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
CloseApplications=force
MinVersion=10.0

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "autostart"; Description: "Iniciar automaticamente no logon de qualquer usuario"
Name: "firewall"; Description: "Liberar o programa no Firewall do Windows (ONVIF, RTSP e descoberta)"

[Files]
Source: "..\dist\ScreenCamONVIF\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\mediamtx.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\LICENSE-mediamtx.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion skipifsourcedoesntexist
Source: "config.template.ini"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{#DataDir}"
Name: "{#DataDir}\recordings"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Editar configuracao"; Filename: "{sys}\notepad.exe"; Parameters: """{#DataDir}\config.ini"""
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ScreenCamONVIF"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF RTSP"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ScreenCam ONVIF"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=any remoteip=localsubnet"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ScreenCam ONVIF RTSP"" dir=in action=allow program=""{app}\mediamtx.exe"" enable=yes profile=any remoteip=localsubnet"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#AppExe}"; Description: "Iniciar o {#AppName} agora"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM mediamtx.exe"; Flags: runhidden; RunOnceId: "KillMtx"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF"""; Flags: runhidden; RunOnceId: "Fw1"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF RTSP"""; Flags: runhidden; RunOnceId: "Fw2"

[Code]
var
  CfgPage: TInputQueryWizardPage;

function ConfigFile: String;
begin
  Result := ExpandConstant('{#DataDir}\config.ini');
end;

procedure InitializeWizard;
begin
  CfgPage := CreateInputQueryPage(wpSelectTasks,
    'Configuracao da camera ONVIF',
    'Dados que o NVR usara para adicionar este computador',
    'O NVR enxergara este computador como uma camera IP. Defina usuario e senha ' +
    '(serao pedidos ao adicionar a camera no NVR).');
  CfgPage.Add('Nome do dispositivo ({computer} = nome do PC):', False);
  CfgPage.Add('Usuario ONVIF/RTSP:', False);
  CfgPage.Add('Senha (minimo 6 caracteres):', True);
  CfgPage.Add('Porta ONVIF (HTTP):', False);
  CfgPage.Add('Porta RTSP:', False);
  CfgPage.Values[0] := 'ScreenCam-{computer}';
  CfgPage.Values[1] := 'admin';
  CfgPage.Values[2] := '';
  CfgPage.Values[3] := '8000';
  CfgPage.Values[4] := '8554';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Em atualizacoes, mantem a configuracao existente }
  Result := (PageID = CfgPage.ID) and FileExists(ConfigFile);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  P1, P2: Integer;
begin
  Result := True;
  if CurPageID = CfgPage.ID then
  begin
    P1 := StrToIntDef(CfgPage.Values[3], 0);
    P2 := StrToIntDef(CfgPage.Values[4], 0);
    if Trim(CfgPage.Values[1]) = '' then
    begin
      MsgBox('Informe o usuario.', mbError, MB_OK);
      Result := False;
    end
    else if Length(CfgPage.Values[2]) < 6 then
    begin
      MsgBox('A senha deve ter ao menos 6 caracteres.', mbError, MB_OK);
      Result := False;
    end
    else if (Pos(';', CfgPage.Values[2]) > 0) or (Pos('#', CfgPage.Values[2]) > 0) then
    begin
      MsgBox('A senha nao pode conter ";" nem "#".', mbError, MB_OK);
      Result := False;
    end
    else if (P1 < 1) or (P1 > 65535) or (P2 < 1) or (P2 > 65535) or (P1 = P2) then
    begin
      MsgBox('Portas invalidas.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  S: AnsiString;
  T: String;
begin
  if (CurStep = ssPostInstall) and not FileExists(ConfigFile) then
  begin
    if LoadStringFromFile(ExpandConstant('{app}\config.template.ini'), S) then
    begin
      T := String(S);
      StringChangeEx(T, '@NAME@', CfgPage.Values[0], True);
      StringChangeEx(T, '@USER@', Trim(CfgPage.Values[1]), True);
      StringChangeEx(T, '@PASS@', CfgPage.Values[2], True);
      StringChangeEx(T, '@ONVIF_PORT@', CfgPage.Values[3], True);
      StringChangeEx(T, '@RTSP_PORT@', CfgPage.Values[4], True);
      SaveStringToFile(ConfigFile, AnsiString(T), False);
    end;
  end;
end;
