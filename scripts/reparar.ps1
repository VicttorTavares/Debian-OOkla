<#
  ScreenCam ONVIF - reparo e diagnostico (execute como ADMINISTRADOR).

  O que faz, em ordem:
    1. localiza a instalacao e mostra versao;
    2. para o servico e roda o diagnostico (doctor) ANTES;
    3. confere se a porta ONVIF esta livre (escolhe alternativa se nao estiver);
    4. regrava o config.ini (NVR, usuario, senha, portas), cifra a senha e trava o arquivo;
    5. recria as regras de firewall (liberadas so para o IP do NVR);
    6. confere o relogio (ONVIF digest falha se PC e NVR divergirem);
    7. reinstala e inicia o servico; se nao subir, executa em primeiro plano para mostrar o erro;
    8. verifica tudo de ponta a ponta e grava um relatorio.

  Exemplo:
    powershell -ExecutionPolicy Bypass -File reparar.ps1 -NvrIp 192.168.25.200 -OnvifPort 80
  A senha e pedida no terminal (nao fica gravada no relatorio).
  Opcional: -Installer "C:\caminho\ScreenCamONVIF-Setup-x.y.z.exe" para atualizar a versao.
#>
param(
    [string]$NvrIp = "",
    [string]$User = "admin",
    [string]$Password = "",
    [int]$OnvifPort = 80,
    [int]$RtspPort = 8554,
    [string]$Installer = "",
    [switch]$NoPause
)
$ErrorActionPreference = "Continue"

# --- precisa ser administrador (reabre elevado se necessario) --------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Reabrindo como Administrador..."
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                 '-User', $User, '-OnvifPort', $OnvifPort, '-RtspPort', $RtspPort)
    if ($NvrIp)     { $argList += @('-NvrIp', $NvrIp) }
    if ($Installer) { $argList += @('-Installer', "`"$Installer`"") }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

# --- relatorio (a senha nunca e registrada) --------------------------------
$logDir = Join-Path $env:ProgramData "ScreenCamONVIF\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$report = Join-Path $logDir ("reparar-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
function Log([string]$m) { Write-Host $m; Add-Content -Path $report -Value $m -Encoding UTF8 }
function Step([string]$m) { Log ""; Log "=== $m ===" }
function Pause-End { if (-not $NoPause) { Read-Host "Pressione Enter para fechar" | Out-Null } }

Log "ScreenCam ONVIF - reparo em $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (computador: $env:COMPUTERNAME)"

# --- dados que faltam -------------------------------------------------------
$ip = $null
while (-not [Net.IPAddress]::TryParse($NvrIp, [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork') {
    $NvrIp = (Read-Host "IP do gravador/NVR (ex.: 192.168.25.200)").Trim()
}
if (-not $Password) {
    $sec = Read-Host "Senha que o NVR usara para acessar esta camera (10 caracteres)" -AsSecureString
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if ($Password.Length -lt 10) { Log "ERRO: a senha deve ter 10 caracteres."; Pause-End; exit 2 }
if ($Password -match '[;#]') { Log "ERRO: a senha nao pode conter ';' nem '#'."; Pause-End; exit 2 }

# --- instalacao -------------------------------------------------------------
Step "1. Instalacao"
$appDir = $null
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B4C1E0F2-5A7D-4C8E-9F3A-2D6B8E1C4A90}_is1'
if (Test-Path $key) { $appDir = (Get-ItemProperty $key).InstallLocation }
if (-not $appDir) { $appDir = Join-Path $env:ProgramFiles "ScreenCamONVIF" }
$appDir = $appDir.TrimEnd('\')
$exe = Join-Path $appDir "ScreenCamONVIF.exe"
$verify = Join-Path $PSScriptRoot "verificar-instalacao.ps1"
if (-not (Test-Path $verify)) { $verify = Join-Path $appDir "tools\verificar-instalacao.ps1" }

if ($Installer) {
    Step "1b. Atualizando com o instalador informado"
    $p = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/FORCECONFIG=1', "/LOG=$logDir\instalador.log",
        "/NVRIP=$NvrIp", "/USER=$User", "/PASS=$Password", "/ONVIFPORT=$OnvifPort", "/RTSPPORT=$RtspPort")
    Log "Instalador terminou com codigo $($p.ExitCode) (log: $logDir\instalador.log)"
    if (-not (Test-Path $exe)) { Log "ERRO: apos a instalacao, $exe nao existe."; Pause-End; exit 1 }
}
if (-not (Test-Path $exe)) {
    Log "ERRO: ScreenCamONVIF.exe nao encontrado em $appDir. Instale primeiro (ou use -Installer)."
    Pause-End; exit 1
}
Log "Pasta: $appDir"
foreach ($f in 'ScreenCamONVIF.exe', 'ffmpeg.exe', 'mediamtx.exe', 'config.template.ini') {
    if (Test-Path (Join-Path $appDir $f)) { Log "  [OK] $f" } else { Log "  [FALHA] $f ausente (reinstale o programa)" }
}
& $exe --version 2>&1 | ForEach-Object { Log "  versao: $_" }

if (-not $Installer) {
    # --- para o servico e diagnostica ---------------------------------------
    Step "2. Parando o servico e diagnostico ANTES do reparo"
    & $exe stop 2>&1 | ForEach-Object { Log "  $_" }
    Get-Process -Name mediamtx -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    & $exe doctor 2>&1 | ForEach-Object { Log "  $_" }

    # --- porta ONVIF ---------------------------------------------------------
    Step "3. Porta ONVIF $OnvifPort"
    function Get-PortOwner([int]$port) {
        $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue) }
        return $null
    }
    $owner = Get-PortOwner $OnvifPort
    if ($owner) {
        Log "  A porta $OnvifPort esta ocupada por '$($owner.ProcessName)' (PID $($owner.Id))."
        Log "  Nao e problema: o servico escolhe uma porta alternativa sozinho e informa qual (veja o resultado ao final)."
        Log "  Se preferir a porta $OnvifPort no NVR, pare '$($owner.ProcessName)' (ex.: IIS: 'iisreset /stop') e rode de novo."
    } else { Log "  Porta livre." }

    # --- config.ini ----------------------------------------------------------
    Step "4. Gravando o config.ini"
    $dataDir = Join-Path $env:ProgramData "ScreenCamONVIF"
    New-Item -ItemType Directory -Force $dataDir, (Join-Path $dataDir "runtime"), (Join-Path $dataDir "recordings") | Out-Null
    $cfg = Join-Path $dataDir "config.ini"
    if (Test-Path $cfg) {
        $bak = "$cfg.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        # liberar a ACL antes de copiar (arquivo travado so para administradores)
        Copy-Item $cfg $bak -Force
        Log "  Backup do config anterior: $bak"
    }
    $t = Get-Content (Join-Path $appDir "config.template.ini") -Raw
    $map = @{
        '@NAME@' = 'ScreenCam-{computer}'; '@USER@' = $User; '@PASS@' = $Password; '@NVR_IP@' = $NvrIp
        '@ONVIF_PORT@' = "$OnvifPort"; '@RTSP_PORT@' = "$RtspPort"
        '@AR_ENABLED@' = 'false'; '@AR_SERVER@' = $NvrIp; '@AR_PORT@' = '7000'; '@AR_DEVID@' = '1'
        '@AR_USER@' = ''; '@AR_PASS@' = ''
    }
    foreach ($k in $map.Keys) { $t = $t.Replace($k, $map[$k]) }
    [IO.File]::WriteAllText($cfg, $t, (New-Object Text.UTF8Encoding($false)))
    & icacls.exe $cfg /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
    & $exe protect-config 2>&1 | ForEach-Object { Log "  $_" }
    Log "  config.ini gravado (NVR $NvrIp, usuario '$User', ONVIF $OnvifPort, RTSP $RtspPort); senha cifrada."

    # --- firewall ------------------------------------------------------------
    Step "5. Firewall (liberado somente para o NVR $NvrIp)"
    foreach ($n in 'ScreenCam ONVIF', 'ScreenCam ONVIF RTSP') {
        Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    }
    New-NetFirewallRule -DisplayName 'ScreenCam ONVIF' -Direction Inbound -Action Allow -Profile Any `
        -Program $exe -RemoteAddress $NvrIp | Out-Null
    New-NetFirewallRule -DisplayName 'ScreenCam ONVIF RTSP' -Direction Inbound -Action Allow -Profile Any `
        -Program (Join-Path $appDir 'mediamtx.exe') -RemoteAddress $NvrIp | Out-Null
    Log "  Regras criadas."

    # --- relogio -------------------------------------------------------------
    Step "6. Relogio (PC e NVR precisam estar com a mesma hora)"
    try { Start-Service w32time -ErrorAction Stop } catch { }
    & w32tm.exe /resync /force 2>&1 | ForEach-Object { Log "  $_" }
    Log "  Hora do PC agora: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (fuso: $((Get-TimeZone).Id))"
    Log "  -> Confira se o NVR mostra a mesma hora/fuso."

    # --- servico -------------------------------------------------------------
    Step "7. Reinstalando e iniciando o servico"
    & $exe uninstall-service 2>&1 | ForEach-Object { Log "  $_" }
    & $exe install-service 2>&1 | ForEach-Object { Log "  $_" }
    & $exe start 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Log "  O servico NAO iniciou. Executando em primeiro plano por 12 s para capturar o erro real..."
        $o = Join-Path $env:TEMP "screencam-out.txt"; $e = Join-Path $env:TEMP "screencam-err.txt"
        $p = Start-Process -FilePath $exe -ArgumentList 'service-console' -PassThru -WindowStyle Hidden `
             -RedirectStandardOutput $o -RedirectStandardError $e
        Start-Sleep -Seconds 12
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
        Get-Process -Name mediamtx -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Log "  ---- saida ----";  Get-Content $o -ErrorAction SilentlyContinue | ForEach-Object { Log "  $_" }
        Log "  ---- erros ----";  Get-Content $e -ErrorAction SilentlyContinue | ForEach-Object { Log "  $_" }
        Log "  ---- eventos do Windows (Service Control Manager) ----"
        Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Service Control Manager' } -MaxEvents 6 -ErrorAction SilentlyContinue |
            ForEach-Object { Log "  $($_.TimeCreated) $($_.Message)" }
    }
}

# --- verificacao ------------------------------------------------------------
Step "8. Verificacao de ponta a ponta"
if (Test-Path $verify) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verify `
        -User $User -Password $Password -AppDir $appDir 2>&1 | ForEach-Object { Log "$_" }
    $ok = ($LASTEXITCODE -eq 0)
    $statusFile = Join-Path $env:ProgramData "ScreenCamONVIF\runtime\status.json"
    if (Test-Path $statusFile) { try { $OnvifPort = [int]((Get-Content $statusFile -Raw | ConvertFrom-Json).onvif_port) } catch { } }
} else { Log "verificar-instalacao.ps1 nao encontrado."; $ok = $false }

Step "Resultado"
if ($ok) {
    Log "SUCESSO. Agora, no NVR (Hikvision): Configuracao > Sistema > Gerenciamento de camera > Adicao personalizada:"
    Log "    Endereco IP : $((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like ($NvrIp.Substring(0, $NvrIp.LastIndexOf('.')) + '.*') } | Select-Object -First 1).IPAddress)  (este PC)"
    Log "    Protocolo   : ONVIF"
    Log "    Porta       : $OnvifPort"
    Log "    Usuario     : $User"
    Log "    Senha       : (a que voce informou)"
} else {
    Log "AINDA HA FALHAS. Envie o relatorio abaixo para suporte:"
}
Log "Relatorio: $report"
Pause-End
if ($ok) { exit 0 } else { exit 1 }
