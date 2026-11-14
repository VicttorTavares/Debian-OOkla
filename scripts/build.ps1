<#
  Gera o instalador do ScreenCam ONVIF no Windows.

  Requisitos: Windows 10/11 x64, Python 3.11+ no PATH, internet.
  Uso (PowerShell):  powershell -ExecutionPolicy Bypass -File scripts\build.ps1
  Resultado:         installer\Output\ScreenCamONVIF-Setup-<versao>.exe
#>
param(
    # Fontes do FFmpeg, tentadas em ordem. A primeira (BtbN, no GitHub) e mais
    # confiavel; o gyan.dev entra como alternativa. Ambas incluem x264/x265.
    [string[]]$FfmpegUrls = @(
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    ),
    [string]$MediaMtxVersion = "v1.21.3",
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# Baixa um arquivo tentando varias URLs, com repeticao e backoff (503/timeout sao transitorios)
function Save-Url {
    param([string[]]$Urls, [string]$OutFile, [int]$Retries = 4)
    foreach ($url in $Urls) {
        for ($i = 1; $i -le $Retries; $i++) {
            try {
                Write-Host "    baixando $url (tentativa $i/$Retries)"
                Invoke-WebRequest $url -OutFile $OutFile -UseBasicParsing -TimeoutSec 300
                return
            } catch {
                Write-Host "    falhou: $($_.Exception.Message)"
                if ($i -lt $Retries) { Start-Sleep -Seconds ([Math]::Min(30, 5 * $i)) }
            }
        }
        Write-Host "    desistindo desta fonte, tentando a proxima..."
    }
    throw "nao foi possivel baixar $OutFile de nenhuma fonte"
}

$version = (Select-String -Path src\screencam\config.py -Pattern 'APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "==> ScreenCam ONVIF $version"

# ---------------------------------------------------------------- binarios externos
New-Item -ItemType Directory -Force vendor | Out-Null
if (-not (Test-Path vendor\ffmpeg.exe)) {
    Write-Host "==> Baixando FFmpeg"
    Save-Url -Urls $FfmpegUrls -OutFile vendor\ffmpeg.zip
    Expand-Archive vendor\ffmpeg.zip vendor\ffmpeg_tmp -Force
    Get-ChildItem vendor\ffmpeg_tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1 | Copy-Item -Destination vendor\ffmpeg.exe
    if (-not (Test-Path vendor\ffmpeg.exe)) { throw "ffmpeg.exe nao encontrado no pacote baixado" }
    Remove-Item vendor\ffmpeg.zip, vendor\ffmpeg_tmp -Recurse -Force
}
if (-not (Test-Path vendor\mediamtx.exe)) {
    Write-Host "==> Baixando MediaMTX $MediaMtxVersion"
    $u = "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip"
    Save-Url -Urls @($u) -OutFile vendor\mediamtx.zip
    Expand-Archive vendor\mediamtx.zip vendor\mediamtx_tmp -Force
    Copy-Item vendor\mediamtx_tmp\mediamtx.exe vendor\mediamtx.exe
    if (Test-Path vendor\mediamtx_tmp\LICENSE) { Copy-Item vendor\mediamtx_tmp\LICENSE vendor\LICENSE-mediamtx.txt }
    Remove-Item vendor\mediamtx.zip, vendor\mediamtx_tmp -Recurse -Force
}

# ---------------------------------------------------------------- ambiente Python
if (-not (Test-Path .venv)) { python -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install falhou" }

# Registra as DLLs do pywin32 (pywintypesXX.dll/pythoncomXX.dll) para o servico empacotar
$pywin32pi = Join-Path $root ".venv\Scripts\pywin32_postinstall.py"
if (Test-Path $pywin32pi) { & $py $pywin32pi -install -silent | Out-Null }

if (-not $SkipTests) {
    Write-Host "==> Testes"
    & $py -m pytest -q tests
    if ($LASTEXITCODE -ne 0) { throw "testes falharam" }
}

# ---------------------------------------------------------------- executavel
Write-Host "==> PyInstaller"
& $py scripts\make_icon.py
# Console (NAO --windowed): os comandos de admin (status/start/stop/protect-config)
# precisam imprimir no terminal. O servico roda na sessao 0 e o agente e lancado com
# CREATE_NO_WINDOW, entao nenhum console aparece para o usuario.
# --collect-submodules screencam: inclui modulos importados tardiamente (service, agent,
# sessions, control, dpapi, onvif.*), que a analise estatica do PyInstaller nao veria.
& $py -m PyInstaller --noconfirm --clean --console --name ScreenCamONVIF `
    --icon installer\icon.ico --paths src `
    --collect-submodules screencam `
    --collect-data soundcard --hidden-import pystray._win32 `
    --hidden-import win32timezone --hidden-import servicemanager `
    --hidden-import win32service --hidden-import win32serviceutil --hidden-import win32event `
    --hidden-import win32api --hidden-import pywintypes --hidden-import pythoncom `
    scripts\launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou" }

# ---------------------------------------------------------------- instalador
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Host "==> Instalando Inno Setup (winget/choco)"
    if (Get-Command choco -ErrorAction SilentlyContinue) { choco install innosetup -y --no-progress | Out-Null }
    else { winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements }
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
Write-Host "==> Inno Setup"
& $iscc "/DAppVersion=$version" installer\screencam.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC falhou" }
Write-Host "==> Pronto: installer\Output\ScreenCamONVIF-Setup-$version.exe"
