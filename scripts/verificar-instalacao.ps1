<#
  Verifica de ponta a ponta se o ScreenCam ONVIF esta funcionando nesta maquina:
  servico, portas, ONVIF (com e sem autenticacao), RTSP, descoberta e logs.

  Uso:  powershell -ExecutionPolicy Bypass -File verificar-instalacao.ps1 `
            -OnvifPort 80 -User admin -Password <senha de 10 caracteres>
  Saida: linhas [OK] / [AVISO] / [FALHA]; codigo de saida 1 se houver falha.
#>
param(
    [int]$OnvifPort = 0,   # 0 = descobrir a porta efetiva (runtime\status.json)
    [int]$RtspPort = 0,
    [string]$User = "admin",
    [string]$Password = "",
    [int]$WaitSeconds = 45,
    [switch]$RequireStream,   # exige video publicado pelo agente (PC com usuario conectado)
    [string]$AppDir = (Join-Path $env:ProgramFiles "ScreenCamONVIF")
)
$ErrorActionPreference = "Continue"
$script:falhas = 0
function Ok($m)   { Write-Host "[OK]    $m" }
function Warn($m) { Write-Host "[AVISO] $m" }
function Bad($m)  { Write-Host "[FALHA] $m"; $script:falhas++ }

Add-Type -AssemblyName System.Net.Http

function Invoke-Soap([string]$url, [string]$xml) {
    $c = New-Object System.Net.Http.HttpClient
    $c.Timeout = [TimeSpan]::FromSeconds(10)
    try {
        $content = New-Object System.Net.Http.StringContent($xml, [Text.Encoding]::UTF8, 'application/soap+xml')
        $r = $c.PostAsync($url, $content).GetAwaiter().GetResult()
        return @{ Status = [int]$r.StatusCode; Body = $r.Content.ReadAsStringAsync().GetAwaiter().GetResult() }
    } catch {
        return @{ Status = 0; Body = $_.Exception.Message }
    } finally { $c.Dispose() }
}

function New-Envelope([string]$body, [string]$header = "") {
    return '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
           $header + '<s:Body>' + $body + '</s:Body></s:Envelope>'
}

# Cabecalho WS-Security UsernameToken com PasswordDigest (igual ao que o NVR envia)
function New-AuthHeader([string]$user, [string]$pass) {
    $nonce = New-Object byte[] 16
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($nonce)
    $created = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $raw = [byte[]]($nonce + [Text.Encoding]::UTF8.GetBytes($created) + [Text.Encoding]::UTF8.GetBytes($pass))
    $digest = [Convert]::ToBase64String([Security.Cryptography.SHA1]::Create().ComputeHash($raw))
    return '<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">' +
        "<UsernameToken><Username>$user</Username>" +
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-username-token-profile-1.0#PasswordDigest">' +
        $digest + '</Password><Nonce>' + [Convert]::ToBase64String($nonce) + '</Nonce>' +
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">' +
        $created + '</Created></UsernameToken></Security></s:Header>'
}

function Wait-TcpListen([int]$port, [int]$secs) {
    $deadline = (Get-Date).AddSeconds($secs)
    do {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { return $true }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return $false
}

Write-Host "== Verificacao do ScreenCam ONVIF =="

# 1) servico ---------------------------------------------------------------
$svc = Get-Service -Name ScreenCamONVIF -ErrorAction SilentlyContinue
if (-not $svc) {
    Bad "Servico ScreenCamONVIF nao esta instalado"
} else {
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ($svc.Status -ne 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2; $svc.Refresh() }
    if ($svc.Status -eq 'Running') { Ok "Servico em execucao" } else { Bad "Servico nao esta em execucao (estado: $($svc.Status))" }
}

# 2) portas ----------------------------------------------------------------
# O servico escolhe uma porta alternativa se a configurada estiver ocupada (ex.: 80 pelo IIS)
$statusFile = Join-Path $env:ProgramData "ScreenCamONVIF\runtime\status.json"
$deadline = (Get-Date).AddSeconds(20)
while (-not (Test-Path $statusFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
$st = $null
if (Test-Path $statusFile) { try { $st = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { } }
if ($st) {
    if ($OnvifPort -eq 0) { $OnvifPort = [int]$st.onvif_port }
    if ($RtspPort -eq 0)  { $RtspPort  = [int]$st.rtsp_port }
    Ok "Portas efetivas do servico: ONVIF=$($st.onvif_port) RTSP=$($st.rtsp_port) codec=$($st.codec) NVR=$($st.nvr_ip)"
    if ($st.wanted_ports.onvif -ne $st.onvif_port) {
        Warn "A porta ONVIF configurada ($($st.wanted_ports.onvif)) estava ocupada; o servico usa $($st.onvif_port). No NVR, use Porta = $($st.onvif_port)."
    }
} else {
    Bad "runtime\status.json nao foi criado (o servico nao chegou a abrir as portas)"
}
if ($OnvifPort -eq 0) { $OnvifPort = 8000 }
if ($RtspPort -eq 0)  { $RtspPort = 8554 }
if (Wait-TcpListen $OnvifPort 20) { Ok "Porta ONVIF $OnvifPort ouvindo" } else { Bad "Porta ONVIF $OnvifPort NAO esta ouvindo" }
if (Wait-TcpListen $RtspPort 30)  { Ok "Porta RTSP $RtspPort ouvindo" }  else { Bad "Porta RTSP $RtspPort NAO esta ouvindo (mediamtx nao iniciou?)" }
if (Get-NetUDPEndpoint -LocalPort 3702 -ErrorAction SilentlyContinue) { Ok "Descoberta (UDP 3702) ativa" } else { Warn "Descoberta (UDP 3702) inativa" }
if (Get-Process -Name mediamtx -ErrorAction SilentlyContinue) { Ok "Processo mediamtx em execucao" } else { Bad "Processo mediamtx NAO esta em execucao" }

# 3) ONVIF -----------------------------------------------------------------
$url = "http://127.0.0.1:$OnvifPort/onvif/device_service"
$r = Invoke-Soap $url (New-Envelope '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
if ($r.Status -eq 200 -and $r.Body -match 'SystemDateAndTime') { Ok "ONVIF responde (GetSystemDateAndTime, sem autenticacao)" }
else { Bad "ONVIF nao respondeu corretamente (HTTP $($r.Status)): $($r.Body)" }

if ($Password) {
    $info = '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    $r = Invoke-Soap $url (New-Envelope $info (New-AuthHeader $User $Password))
    if ($r.Status -eq 200 -and $r.Body -match 'Manufacturer') { Ok "ONVIF autenticado com usuario '$User' (GetDeviceInformation)" }
    else { Bad "Autenticacao ONVIF com '$User' falhou (HTTP $($r.Status)). Confira usuario/senha do config.ini" }
    $r = Invoke-Soap $url (New-Envelope $info (New-AuthHeader $User ($Password + "x")))
    if ($r.Status -ne 200) { Ok "Senha errada e recusada (seguranca ok)" } else { Bad "Senha errada foi ACEITA" }

    $prof = '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
    $r = Invoke-Soap "http://127.0.0.1:$OnvifPort/onvif/media_service" (New-Envelope $prof (New-AuthHeader $User $Password))
    if ($r.Status -eq 200 -and $r.Body -match 'Profile_1') { Ok "Perfil de midia disponivel (Media1)" } else { Bad "GetProfiles falhou (HTTP $($r.Status))" }
    $prof2 = '<GetProfiles xmlns="http://www.onvif.org/ver20/media/wsdl"><Type>All</Type></GetProfiles>'
    $r = Invoke-Soap "http://127.0.0.1:$OnvifPort/onvif/media2_service" (New-Envelope $prof2 (New-AuthHeader $User $Password))
    if ($r.Status -eq 200 -and $r.Body -match 'H26[45]') { Ok "Media2 anuncia codec: $(([regex]::Match($r.Body,'H26[45]')).Value)" } else { Bad "Media2 GetProfiles falhou (HTTP $($r.Status))" }
} else {
    Warn "Senha nao informada: pulei os testes autenticados (use -Password)"
}

# 4) RTSP (OPTIONS) --------------------------------------------------------
try {
    $tcp = New-Object Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", $RtspPort)
    $stream = $tcp.GetStream(); $stream.ReadTimeout = 5000
    $req = [Text.Encoding]::ASCII.GetBytes("OPTIONS rtsp://127.0.0.1:$RtspPort/screen RTSP/1.0`r`nCSeq: 1`r`n`r`n")
    $stream.Write($req, 0, $req.Length)
    $buf = New-Object byte[] 512
    $n = $stream.Read($buf, 0, 512)
    $resp = [Text.Encoding]::ASCII.GetString($buf, 0, $n)
    if ($resp -match '^RTSP/1.0 200') { Ok "Servidor RTSP responde (OPTIONS 200)" } else { Bad "RTSP respondeu algo inesperado: $($resp.Split("`n")[0])" }
    $tcp.Close()
} catch { Bad "RTSP nao respondeu: $($_.Exception.Message)" }

# 4b) stream publicado pelo agente de captura (DESCRIBE) ---------------------
function Get-RtspDescribe([int]$port) {
    try {
        $tcp = New-Object Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $port)
        $stream = $tcp.GetStream(); $stream.ReadTimeout = 5000
        $req = [Text.Encoding]::ASCII.GetBytes("DESCRIBE rtsp://127.0.0.1:$port/screen RTSP/1.0`r`nCSeq: 2`r`nAccept: application/sdp`r`n`r`n")
        $stream.Write($req, 0, $req.Length)
        Start-Sleep -Milliseconds 500
        $buf = New-Object byte[] 4096
        $n = $stream.Read($buf, 0, 4096)
        $tcp.Close()
        return [Text.Encoding]::ASCII.GetString($buf, 0, $n)
    } catch { return "" }
}
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$sdp = ""
do {
    $sdp = Get-RtspDescribe $RtspPort
    if ($sdp -match '^RTSP/1.0 200') { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if ($sdp -match '^RTSP/1.0 200') {
    $v = if ($sdp -match 'm=video') { "video" } else { "SEM video" }
    $a = if ($sdp -match 'm=audio') { "audio" } else { "sem audio" }
    $c = if ($sdp -match 'H265|HEVC') { "H.265" } elseif ($sdp -match 'H264') { "H.264" } else { "codec ?" }
    Ok "Stream publicado pelo agente de captura: $v + $a ($c)"
} else {
    $msg = "Nenhum stream publicado em rtsp://127.0.0.1:$RtspPort/screen (o agente de captura nao esta enviando video). " +
           "Isso e esperado se NAO houver usuario conectado na area de trabalho."
    if ($RequireStream) { Bad $msg } else { Warn $msg }
}
# saida (stdout/stderr) do agente, capturada pelo servico: mostra ate falhas muito precoces
$agentLogs = @(Get-ChildItem (Join-Path $env:ProgramData "ScreenCamONVIF\logs\agente-sessao-*.log") -ErrorAction SilentlyContinue)
$agentLogs += @(Get-ChildItem "C:\Users\*\AppData\Local\ScreenCamONVIF\logs\agent.log" -ErrorAction SilentlyContinue)
foreach ($al in $agentLogs) {
    Write-Host "---- $($al.FullName) (ultimas 25 linhas) ----"
    Get-Content $al.FullName -Tail 25 -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
}
if ($agentLogs.Count -eq 0) { Warn "Nenhum agent.log encontrado (o agente nao chegou a rodar em nenhuma sessao de usuario)" }

# 5) logs ------------------------------------------------------------------
$log = Join-Path $env:ProgramData "ScreenCamONVIF\logs\service.log"
if (Test-Path $log) {
    $tail = Get-Content $log -Tail 400 -Encoding UTF8 -ErrorAction SilentlyContinue
    # Falha real = excecao nao tratada. Mensagens ERROR conhecidas (porta ocupada -> alternativa) sao avisos.
    $fatais = @($tail | Where-Object { $_ -match 'Traceback|Erro fatal|Nenhuma porta ONVIF' })
    $erros = @($tail | Where-Object { $_ -match ' ERROR ' })
    if ($fatais.Count -gt 0) { Bad "Log do servico contem falha grave (ultima: $($fatais[-1]))" }
    else { Ok "Log do servico sem falhas graves" }
    foreach ($e in ($erros | Select-Object -Last 3)) { Warn "Log do servico: $e" }
    Write-Host "---- service.log (ultimas 14 linhas) ----"
    $tail | Select-Object -Last 14 | ForEach-Object { Write-Host "    $_" }
} else {
    Bad "Log do servico nao existe em $log (o servico nunca chegou a iniciar)"
}

if ($script:falhas -gt 0) {
    Write-Host "== RESULTADO: $($script:falhas) FALHA(S) =="
    exit 1
}
Write-Host "== RESULTADO: tudo funcionando =="
exit 0
