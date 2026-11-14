<#
  Verifica de ponta a ponta se o ScreenCam ONVIF esta funcionando nesta maquina:
  servico, portas, ONVIF (com e sem autenticacao), RTSP, descoberta e logs.

  Uso:  powershell -ExecutionPolicy Bypass -File verificar-instalacao.ps1 `
            -OnvifPort 80 -User admin -Password <senha de 10 caracteres>
  Saida: linhas [OK] / [AVISO] / [FALHA]; codigo de saida 1 se houver falha.
#>
param(
    [int]$OnvifPort = 8000,
    [int]$RtspPort = 8554,
    [string]$User = "admin",
    [string]$Password = "",
    [int]$WaitSeconds = 45,
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

Write-Host "== Verificacao do ScreenCam ONVIF (ONVIF=$OnvifPort RTSP=$RtspPort) =="

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

# 5) logs ------------------------------------------------------------------
$log = Join-Path $env:ProgramData "ScreenCamONVIF\logs\service.log"
if (Test-Path $log) {
    $tail = Get-Content $log -Tail 400 -ErrorAction SilentlyContinue
    $erros = @($tail | Where-Object { $_ -match 'Traceback|ERROR|Erro fatal' })
    if ($erros.Count -gt 0) { Bad "Log do servico contem erros (ultimos: $($erros[-1]))" } else { Ok "Log do servico sem erros" }
    Write-Host "---- service.log (ultimas 12 linhas) ----"
    $tail | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" }
} else {
    Bad "Log do servico nao existe em $log (o servico nunca chegou a iniciar)"
}

if ($script:falhas -gt 0) {
    Write-Host "== RESULTADO: $($script:falhas) FALHA(S) =="
    exit 1
}
Write-Host "== RESULTADO: tudo funcionando =="
exit 0
