; Instalador do ScreenCam ONVIF (Inno Setup 6)
; Compilar com: ISCC.exe installer\screencam.iss  (o build.ps1 faz isso)

#define AppName "ScreenCam ONVIF"
#ifndef AppVersion
  #define AppVersion "1.5.0"
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
Name: "firewall"; Description: "Liberar o programa no Firewall do Windows (ONVIF, RTSP e descoberta)"

[Files]
Source: "..\dist\ScreenCamONVIF\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\mediamtx.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\LICENSE-mediamtx.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion skipifsourcedoesntexist
Source: "config.template.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\reparar.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\scripts\verificar-instalacao.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\docs\GUIA-DE-INSTALACAO.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Dirs]
Name: "{#DataDir}"
Name: "{#DataDir}\recordings"
Name: "{#DataDir}\runtime"; Permissions: users-readexec

[Icons]
; Atalhos administrativos (o programa roda como servico; nao ha janela para o usuario comum)
Name: "{group}\Reparar e diagnosticar (admin)"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\reparar.ps1"""
Name: "{group}\Guia de instalacao"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\docs\GUIA-DE-INSTALACAO.md"""
Name: "{group}\Editar configuracao (admin)"; Filename: "{sys}\notepad.exe"; Parameters: """{#DataDir}\config.ini"""
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF RTSP"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ScreenCam ONVIF"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=any remoteip={code:GetFirewallScope}"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ScreenCam ONVIF RTSP"" dir=in action=allow program=""{app}\mediamtx.exe"" enable=yes profile=any remoteip={code:GetFirewallScope}"; Flags: runhidden; Tasks: firewall

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "SvcRemove"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM mediamtx.exe"; Flags: runhidden; RunOnceId: "KillMtx"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF"""; Flags: runhidden; RunOnceId: "Fw1"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ScreenCam ONVIF RTSP"""; Flags: runhidden; RunOnceId: "Fw2"

[Code]
const
  F_NAME = 0;
  F_USER = 1;
  F_PASS = 2;
  F_CONFIRM = 3;
  F_NVRIP = 4;
  F_ONVIF = 5;
  F_RTSP = 6;

var
  CfgPage: TInputQueryWizardPage;
  ArPage: TInputQueryWizardPage;
  ServiceOk: Boolean;

{ Le parametros de linha de comando no formato /NOME=valor (instalacao silenciosa) }
function GetCmdParam(const Name, Default: String): String;
var
  i: Integer;
  P, Prefix: String;
begin
  Result := Default;
  Prefix := UpperCase(Name) + '=';
  for i := 1 to ParamCount do
  begin
    P := ParamStr(i);
    if Pos(Prefix, UpperCase(P)) = 1 then
    begin
      Result := Copy(P, Length(Prefix) + 1, MaxInt);
      Exit;
    end;
  end;
end;

function ForceConfig: Boolean;
begin
  Result := GetCmdParam('/FORCECONFIG', '') = '1';
end;

function RunApp(const Params: String): Integer;
var
  Code: Integer;
begin
  if not Exec(ExpandConstant('{app}\{#AppExe}'), Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Log(Format('{#AppExe} %s -> codigo %d', [Params, Code]));
  Result := Code;
end;

procedure RunQuiet(const Exe, Params: String);
var
  Code: Integer;
begin
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Log(Format('%s %s -> codigo %d', [Exe, Params, Code]));
end;

function ConfigFile: String;
begin
  Result := ExpandConstant('{#DataDir}\config.ini');
end;

{ Registro ativo habilitado quando ha IP do NVR e porta de registro informados }
function ArEnabled: Boolean;
begin
  Result := (Trim(CfgPage.Values[F_NVRIP]) <> '') and (Trim(ArPage.Values[0]) <> '');
end;

{ Escopo do firewall: so o IP do NVR, ou toda a sub-rede local se vazio }
function GetFirewallScope(Param: String): String;
begin
  if Trim(CfgPage.Values[F_NVRIP]) <> '' then
    Result := Trim(CfgPage.Values[F_NVRIP])
  else
    Result := 'localsubnet';
end;

function IsValidIPv4(S: String): Boolean;
var
  Parts: TStringList;
  i, n: Integer;
begin
  Result := False;
  Parts := TStringList.Create;
  try
    Parts.Delimiter := '.';
    Parts.StrictDelimiter := True;
    Parts.DelimitedText := S;
    if Parts.Count <> 4 then Exit;
    for i := 0 to 3 do
    begin
      if Parts[i] = '' then Exit;
      n := StrToIntDef(Parts[i], -1);
      if (n < 0) or (n > 255) then Exit;
    end;
    Result := True;
  finally
    Parts.Free;
  end;
end;

procedure InitializeWizard;
begin
  CfgPage := CreateInputQueryPage(wpSelectTasks,
    'Configuracao da camera ONVIF',
    'Dados que o NVR usara para adicionar este computador',
    'O NVR enxergara este computador como uma camera IP. Defina usuario e senha ' +
    '(serao pedidos ao adicionar a camera no NVR). A senha deve ter 10 caracteres.');
  CfgPage.Add('Nome do dispositivo ({computer} = nome do PC):', False);
  CfgPage.Add('Usuario ONVIF/RTSP:', False);
  CfgPage.Add('Senha (10 caracteres):', True);
  CfgPage.Add('Confirmar senha:', True);
  CfgPage.Add('IP do NVR/gravador (opcional; restringe o acesso a ele):', False);
  CfgPage.Add('Porta ONVIF (informe esta porta como "gerenciamento" no NVR):', False);
  CfgPage.Add('Porta RTSP:', False);
  { Valores padrao; podem ser sobrescritos por /NAME= /USER= /PASS= /NVRIP= /ONVIFPORT= /RTSPPORT= }
  CfgPage.Values[F_NAME] := GetCmdParam('/NAME', 'ScreenCam-{computer}');
  CfgPage.Values[F_USER] := GetCmdParam('/USER', 'admin');
  CfgPage.Values[F_PASS] := GetCmdParam('/PASS', '');
  CfgPage.Values[F_CONFIRM] := GetCmdParam('/PASS', '');
  CfgPage.Values[F_NVRIP] := GetCmdParam('/NVRIP', '');
  CfgPage.Values[F_ONVIF] := GetCmdParam('/ONVIFPORT', '8000');
  CfgPage.Values[F_RTSP] := GetCmdParam('/RTSPPORT', '8554');

  ArPage := CreateInputQueryPage(CfgPage.ID,
    'Registro ativo no NVR (opcional)',
    'Use apenas se a busca automatica nao encontrar a camera',
    'Se o NVR e o computador estao em sub-redes diferentes (ex.: 192.168.25.x e ' +
    '192.168.26.x) ou ha NAT, a busca automatica falha. Neste caso o dispositivo ' +
    'pode CONECTAR no NVR (tela "End. servidor / ID dispositivo"). Deixe a porta em ' +
    'branco para nao usar. (Recurso experimental; confira o IP do NVR na tela anterior.)');
  ArPage.Add('Porta do Registro Automatico no NVR (em branco = nao usar):', False);
  ArPage.Add('ID do dispositivo (no NVR):', False);
  ArPage.Add('Usuario no NVR (opcional):', False);
  ArPage.Add('Senha no NVR (opcional):', True);
  ArPage.Values[0] := GetCmdParam('/ARPORT', '');
  ArPage.Values[1] := GetCmdParam('/ARDEVID', '1');
  ArPage.Values[2] := GetCmdParam('/ARUSER', '');
  ArPage.Values[3] := GetCmdParam('/ARPASS', '');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Em atualizacoes, mantem a configuracao existente }
  Result := ((PageID = CfgPage.ID) or (PageID = ArPage.ID)) and FileExists(ConfigFile) and (not ForceConfig);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  P1, P2: Integer;
begin
  Result := True;
  if CurPageID = CfgPage.ID then
  begin
    P1 := StrToIntDef(CfgPage.Values[F_ONVIF], 0);
    P2 := StrToIntDef(CfgPage.Values[F_RTSP], 0);
    if Trim(CfgPage.Values[F_USER]) = '' then
    begin
      MsgBox('Informe o usuario.', mbError, MB_OK);
      Result := False;
    end
    else if Length(CfgPage.Values[F_PASS]) < 10 then
    begin
      MsgBox('A senha deve ter 10 caracteres (exigencia do gravador).', mbError, MB_OK);
      Result := False;
    end
    else if CfgPage.Values[F_PASS] <> CfgPage.Values[F_CONFIRM] then
    begin
      MsgBox('A senha e a confirmacao nao coincidem.', mbError, MB_OK);
      Result := False;
    end
    else if (Pos(';', CfgPage.Values[F_PASS]) > 0) or (Pos('#', CfgPage.Values[F_PASS]) > 0) then
    begin
      MsgBox('A senha nao pode conter ";" nem "#".', mbError, MB_OK);
      Result := False;
    end
    else if (Trim(CfgPage.Values[F_NVRIP]) <> '') and (not IsValidIPv4(Trim(CfgPage.Values[F_NVRIP]))) then
    begin
      MsgBox('IP do NVR invalido. Use um IPv4 como 192.168.1.50, ou deixe em branco.', mbError, MB_OK);
      Result := False;
    end
    else if (P1 < 1) or (P1 > 65535) or (P2 < 1) or (P2 > 65535) or (P1 = P2) then
    begin
      MsgBox('Portas invalidas.', mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = ArPage.ID then
  begin
    if Trim(ArPage.Values[0]) <> '' then
    begin
      P1 := StrToIntDef(Trim(ArPage.Values[0]), 0);
      if (P1 < 1) or (P1 > 65535) then
      begin
        MsgBox('Porta do Registro Automatico invalida (ou deixe em branco).', mbError, MB_OK);
        Result := False;
      end
      else if Trim(CfgPage.Values[F_NVRIP]) = '' then
      begin
        MsgBox('Para usar o registro ativo, informe o IP do NVR na tela anterior.', mbError, MB_OK);
        Result := False;
      end;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { No modo silencioso as validacoes das paginas nao rodam: valida aqui }
  if WizardSilent and ((not FileExists(ConfigFile)) or ForceConfig) then
  begin
    if Length(CfgPage.Values[F_PASS]) < 10 then
      Result := 'Informe /PASS= com 10 caracteres (exigencia do gravador).'
    else if (Trim(CfgPage.Values[F_NVRIP]) <> '') and (not IsValidIPv4(Trim(CfgPage.Values[F_NVRIP]))) then
      Result := 'Valor invalido em /NVRIP= (use um IPv4, ex.: 192.168.25.200).';
  end;
  if Result <> '' then Exit;
  { Libera os arquivos: para o servico e processos antes de atualizar/reinstalar }
  RunQuiet(ExpandConstant('{sys}\sc.exe'), 'stop ScreenCamONVIF');
  Sleep(2500);
  RunQuiet(ExpandConstant('{sys}\taskkill.exe'), '/F /IM mediamtx.exe');
  RunQuiet(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  S: AnsiString;
  T: String;
begin
  if (CurStep = ssPostInstall) and ((not FileExists(ConfigFile)) or ForceConfig) then
  begin
    if LoadStringFromFile(ExpandConstant('{app}\config.template.ini'), S) then
    begin
      T := String(S);
      StringChangeEx(T, '@NAME@', CfgPage.Values[F_NAME], True);
      StringChangeEx(T, '@USER@', Trim(CfgPage.Values[F_USER]), True);
      StringChangeEx(T, '@PASS@', CfgPage.Values[F_PASS], True);
      StringChangeEx(T, '@NVR_IP@', Trim(CfgPage.Values[F_NVRIP]), True);
      StringChangeEx(T, '@ONVIF_PORT@', CfgPage.Values[F_ONVIF], True);
      StringChangeEx(T, '@RTSP_PORT@', CfgPage.Values[F_RTSP], True);
      { registro ativo no NVR }
      if ArEnabled then
        StringChangeEx(T, '@AR_ENABLED@', 'true', True)
      else
        StringChangeEx(T, '@AR_ENABLED@', 'false', True);
      StringChangeEx(T, '@AR_SERVER@', Trim(CfgPage.Values[F_NVRIP]), True);
      if Trim(ArPage.Values[0]) <> '' then
        StringChangeEx(T, '@AR_PORT@', Trim(ArPage.Values[0]), True)
      else
        StringChangeEx(T, '@AR_PORT@', '7000', True);
      if Trim(ArPage.Values[1]) <> '' then
        StringChangeEx(T, '@AR_DEVID@', Trim(ArPage.Values[1]), True)
      else
        StringChangeEx(T, '@AR_DEVID@', '1', True);
      StringChangeEx(T, '@AR_USER@', Trim(ArPage.Values[2]), True);
      StringChangeEx(T, '@AR_PASS@', ArPage.Values[3], True);
      SaveStringToFile(ConfigFile, AnsiString(T), False);
    end;
  end;
  if CurStep = ssPostInstall then
  begin
    { config.ini contem a senha do NVR: so SYSTEM e Administradores leem }
    RunQuiet(ExpandConstant('{sys}\icacls.exe'),
      '"' + ConfigFile + '" /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F"');
    RunApp('protect-config');
    ServiceOk := (RunApp('install-service') = 0) and (RunApp('start') = 0);
    if ServiceOk then
      Log('Servico instalado e em execucao.')
    else
      Log('ATENCAO: o servico nao ficou em execucao.');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (not ServiceOk) and (not WizardSilent) then
    SuppressibleMsgBox('A instalacao terminou, mas o SERVICO nao ficou em execucao.' + #13#10 + #13#10 +
      'Abra o menu Iniciar > ScreenCam ONVIF > "Reparar e diagnosticar (admin)" ' +
      'ou consulte o guia em ' + ExpandConstant('{app}\docs\GUIA-DE-INSTALACAO.md') + '.',
      mbError, MB_OK, IDOK);
end;
