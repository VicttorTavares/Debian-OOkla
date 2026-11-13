# ScreenCam ONVIF

Software para Windows que transforma o computador em uma **câmera IP ONVIF**:
a tela (com **áudio** do sistema e do microfone) é transmitida por RTSP, e cada
**movimento do mouse ou tecla pressionada** gera um evento de **"detecção de
movimento"** para o NVR. Com isso, o NVR grava exatamente os períodos em que o
usuário está usando o computador, como faz com uma câmera de verdade.

```
 ┌───────────────────────────── Computador Windows ──────────────────────────────┐
 │                                                                               │
 │  Sessão do usuário (João, Maria, ...)        Serviço SYSTEM (oculto, só admin)│
 │  ┌─────────────── Agente de captura ─────┐   ┌──────────── ServerCore ───────┐│
 │  │ Tela (gdigrab) ─┐                     │   │                               ││
 │  │ Áudio WASAPI  ──┼─► FFmpeg (H.265 +   │──►│ MediaMTX (RTSP) ◄─────────────┼┼─ NVR :8554
 │  │ (sistema+mic)   │   AAC/G.711) ───────┼─┐ │ Servidor ONVIF ◄──────────────┼┼─ NVR :8000
 │  │ GetLastInputInfo├─► atividade ────────┼─┼►│ EventBroker (Motion)          ││
 │  └─────────────────┘      (loopback)     │ │ │ WS-Discovery ◄────────────────┼┼─ NVR :3702
 │        ▲  o serviço injeta/reinicia      │ │ │ Gravação local (backup)       ││
 │        └── SessionManager (WTS) ─────────┼─┘ └───────────────────────────────┘│
 │           segue a sessão ativa / troca de usuário                             │
 └───────────────────────────────────────────────────────────────────────────────┘
```

O **serviço** (conta SYSTEM) hospeda RTSP/ONVIF/descoberta/gravação e guarda a senha;
o usuário comum não consegue pará-lo nem lê-lo. Como a sessão 0 do Windows não enxerga
a área de trabalho do usuário, um **agente de captura** é injetado pelo serviço em cada
sessão de usuário e reiniciado automaticamente se for encerrado. Em PCs compartilhados,
o agente segue a sessão ativa — todos que usam a máquina são gravados.

## Funcionalidades

| Recurso | Detalhe |
|---|---|
| Câmera ONVIF (Profile S/T) | Device, Media, **Media2** (anuncia H.265) e Events. Testado com cliente ONVIF real (`onvif-zeep-async`, usado pelo Home Assistant) |
| Descoberta automática | WS-Discovery: o NVR encontra o PC em "Buscar dispositivos" |
| Vídeo | **H.265/HEVC** (padrão) ou H.264, até 1920×1080, 15 fps, VBR otimizado para tela. GPU NVIDIA/Intel/AMD detectada automaticamente |
| Áudio | Som do sistema (loopback) + microfone, mixados. AAC 48 kHz ou G.711 µ-law 8 kHz |
| Gatilho por atividade | 1º movimento do mouse ou tecla → `IsMotion=true`. Após N s ocioso → `IsMotion=false` |
| Eventos | `tns1:RuleEngine/CellMotionDetector/Motion` e `tns1:VideoSource/MotionAlarm` via PullPoint e Notify (push) |
| Snapshot | `GetSnapshotUri` (JPEG) para miniaturas no NVR |
| Segurança | WS-UsernameToken (digest), HTTP Digest/Basic; RTSP com usuário/senha |
| Carimbo no vídeo | Computador, usuário, data e hora sobre a imagem (útil como prova) |
| Gravação local (opcional) | Backup em MKV somente durante a atividade, com retenção por dias/espaço |
| Serviço do Windows | Roda como SYSTEM, inicia no boot, reinicia sozinho; **o usuário comum não fecha** |
| Multiusuário | Um agente por sessão; segue a troca de usuário ou grava várias sessões RDP |
| Discrição | Sem janela/ícone por padrão (`visible_indicator = false`); **não registra o que é digitado** |

## Instalação (administrador)

1. Execute `ScreenCamONVIF-Setup-1.2.0.exe` como administrador.
2. No assistente, informe **nome do dispositivo, usuário, senha, porta ONVIF e porta RTSP**.
3. Deixe marcada a opção *Liberar no Firewall*.
4. O instalador registra e inicia o **serviço do Windows** `ScreenCamONVIF` (início automático).
   Não há ícone para o usuário comum.

A configuração fica em `C:\ProgramData\ScreenCamONVIF\config.ini` (legível **apenas por
administradores**, pois contém a senha do NVR) e os logs do serviço em
`C:\ProgramData\ScreenCamONVIF\logs`.

### Controle (apenas administrador)

```
ScreenCamONVIF.exe status            estado do serviço
ScreenCamONVIF.exe stop  / start     parar / iniciar (reinicie após editar o config.ini)
ScreenCamONVIF.exe uninstall-service remover o serviço
ScreenCamONVIF.exe console           rodar em primeiro plano, 1 usuário (teste/diagnóstico)
ScreenCamONVIF.exe service-console   rodar o serviço + agentes em primeiro plano (diagnóstico)
```

O serviço é protegido pelo Windows: parar/remover exige privilégio de administrador.
Um usuário comum que encerre o agente de captura pelo Gerenciador de Tarefas o verá
reiniciar em poucos segundos.

## Configuração no NVR

1. **Adicionar câmera**: protocolo **ONVIF**, IP do computador, porta **8000**, usuário/senha
   definidos na instalação (ou use a busca automática).
   Se o NVR pedir a URL RTSP diretamente: `rtsp://IP-DO-PC:8554/screen`.
2. **Gravação por movimento**: em *Agenda de gravação*, selecione **Movimento/Evento**
   para o canal. Configure pré-gravação (ex.: 5 s) e pós-gravação (ex.: 30 s).
3. **Codec**: no canal, deixe o stream principal em **H.265**. Se o NVR tiver a opção
   "ONVIF Media2"/"Profile T", ative-a.
4. **Áudio**: habilite o áudio no canal (*Stream principal → Tipo: Vídeo e Áudio*).
   Se o NVR não reproduzir o áudio, altere `codec = g711` no `config.ini`.
5. Em alguns NVRs é preciso ativar *"Detecção de movimento"* / *"Eventos ONVIF"* no canal.

### Intelbras e Hikvision

Os dois emitem os tópicos padrão que o programa gera (`CellMotionDetector/Motion` e
`VideoSource/MotionAlarm`, via PullPoint), então a gravação por movimento funciona como
em qualquer câmera ONVIF.

- **Intelbras** (NVD/MHDX/iNVD): *Adicionar Dispositivo → Buscar* (ONVIF), ou manual com
  *Fabricante: ONVIF*, porta **8000**. Em *Evento → Detecção de Movimento* marque o canal e
  ligue a gravação por movimento na agenda. H.265 é aceito nos modelos atuais; nos antigos,
  use `codec = h264`. Áudio: *Codec de Áudio* no canal; se não tocar, `codec = g711`.
- **Hikvision** (DS-7xxx/iDS, Hik-Connect): *Configuração → Câmera → adicionar* com
  *Protocolo: ONVIF*, porta **8000**. O Hikvision lê o codec pelo Media2 (H.265 ok).
  Ative *Evento → Evento Básico → Detecção de Movimento* no canal e, na agenda de gravação,
  selecione *Evento/Movimento*. Dica: se os eventos ONVIF não chegarem, confirme que o
  usuário ONVIF criado no NVR tem permissão de evento e que data/hora do PC e do NVR batem.

> Em ambos, se o NVR só gravar "contínuo", ele ainda grava — mas sem a economia do gatilho
> por movimento. Vale conferir se o canal está em modo *Evento*.

## Compressão H.265 e economia de armazenamento

O vídeo sai em **H.265 (HEVC)** por padrão. O ONVIF original (Media1) não tem H.265 na
lista de codecs, por isso o programa também implementa o serviço **Media2**, que é por
onde os NVRs com suporte a H.265 descobrem o codec.

A codificação foi ajustada para conteúdo de tela (texto nítido, muita área parada):

- **VBR por qualidade (CRF/CQ) com teto**: tela parada quase não consome bits.
- **GOP de 4 s** (antes eram 2 s): em tela parada, o que mais ocupa disco são os
  quadros-chave completos, e não o movimento.
- **Lookahead de ~1 s** (acrescenta ~1 s de atraso ao vivo, sem importância para gravação).
- **Encoder em GPU** quando houver (NVENC, Quick Sync, AMF). Na CPU usa x265 `faster`.

Medição em tela 1080p de escritório (documento com rolagens ocasionais e mouse se
movendo), com a mesma qualidade visual (SSIM ≥ 0,999), codificando na CPU:

| Configuração | Bitrate médio | Por hora gravada |
|---|---|---|
| v1.0 (H.264, GOP 2 s) | 920 kbps | ~410 MB |
| H.264 otimizado (GOP 4 s + lookahead) | 451 kbps | ~200 MB |
| **v1.1 padrão (H.265, GOP 4 s + lookahead)** | **425 kbps** | **~190 MB** |
| H.264, GOP 10 s | 311 kbps | ~140 MB |

> Em conteúdo de tela, o ganho do H.265 sozinho sobre um H.264 bem ajustado ficou em torno
> de 5–20% na CPU. A maior parte da economia vem do GOP maior e do VBR. Encoders HEVC em GPU
> costumam ter vantagem maior sobre o H.264 da mesma GPU. Telas com vídeo ou muita
> rolagem atingem o teto (`bitrate_kbps`). Com gravação **por movimento** no NVR, só os
> períodos de uso entram na conta.

**Para economizar ainda mais:** `gop_seconds = 8` (ao vivo demora até 8 s para abrir),
`quality = 30` (imagem um pouco mais suave), `fps = 10` ou `max_width/max_height` menores.

**Se o NVR não suportar H.265** (imagem preta ou "codec não suportado"), use `codec = h264`.
Se o FFmpeg/GPU não tiver encoder HEVC, o programa cai para H.264 sozinho e informa isso ao NVR.

> **Atualização da v1.0:** o `config.ini` existente é mantido. Sem a chave `codec`, o padrão
> passa a ser H.265, e `encoder = libx264` antigo é entendido como "CPU". Se o NVR for antigo,
> acrescente `codec = h264` na seção `[video]`.

## Configuração (`config.ini`)

Principais opções (o arquivo completo é comentado):

| Seção | Chave | Padrão | Descrição |
|---|---|---|---|
| `[video]` | `monitor` | `all` | `all` ou número do monitor (1 = principal) |
| | `codec` | `h265` | `h265` ou `h264` |
| | `encoder` | `auto` | `auto`, `cpu`, `nvenc`, `qsv`, `amf` |
| | `rate_control` / `quality` | `vbr` / `0` (auto) | VBR por qualidade ou CBR |
| | `fps` / `bitrate_kbps` | `15` / `1500` | Taxa de quadros e teto de bitrate |
| | `gop_seconds` | `4` | Intervalo entre quadros-chave (1 a 10) |
| | `overlay` | `true` | Carimbo computador/usuário/data-hora |
| `[audio]` | `system_audio` / `microphone` | `true` | Fontes de áudio |
| | `codec` | `aac` | `aac` ou `g711` |
| `[activity]` | `idle_timeout_seconds` | `30` | Tempo sem atividade para encerrar o evento |
| | `stream_mode` | `continuous` | `on_activity` só transmite durante a atividade |
| `[recording]` | `enabled` | `false` | Gravação local de backup |
| `[network]` | `advertise_ip` | `auto` | Fixe o IP se o PC tiver várias placas/VPN |
| `[service]` | `mode` | `service` | `service` (SYSTEM + agente) ou `console` (1 processo) |
| | `capture_scope` | `active_console` | `active_console` ou `all_sessions` (RDP) |
| | `visible_indicator` | `false` | Ícone discreto na bandeja do usuário |

## Multiusuário e sessões

O requisito "todos os usuários da máquina são gravados" é atendido pelo **gerenciador de
sessões** do serviço:

- **`active_console`** (padrão): grava quem estiver usando o monitor físico. Em PCs
  compartilhados com **troca de usuário** (ou logins sequenciais), o agente é movido para
  a sessão que ficou ativa — João sai, Maria entra, a gravação segue a Maria. É sempre
  **uma câmera por PC** no NVR.
- **`all_sessions`**: para **várias sessões RDP simultâneas**, um agente por sessão. Hoje
  todas publicam no mesmo caminho RTSP (o NVR vê uma câmera por vez); múltiplos canais ONVIF
  simultâneos estão na lista de melhorias. Para Terminal Server com muitos usuários ao mesmo
  tempo, prefira avaliar caso a caso.

O serviço injeta o agente via `WTSQueryUserToken` + `CreateProcessAsUser` (área de trabalho
`winsta0\default`) e o reinicia se cair. O agente só recebe uma configuração **sem senhas**
(`ProgramData\ScreenCamONVIF\runtime\agent.json`); as credenciais do NVR ficam só no serviço.

## Gerar o instalador (desenvolvedor)

No Windows 10/11 com Python 3.11+:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
# => installer\Output\ScreenCamONVIF-Setup-1.2.0.exe
```

O script baixa o **FFmpeg** (gyan.dev) e o **MediaMTX v1.21.3**, roda os testes, gera o
executável com PyInstaller e compila o instalador com Inno Setup. O workflow
`.github/workflows/build-windows.yml` faz o mesmo no GitHub Actions e publica o
instalador como artefato.

Para executar a partir do código-fonte: `pip install -r requirements.txt`, coloque
`ffmpeg.exe` e `mediamtx.exe` em `vendor\` e rode `python -m screencam console` dentro de
`src\`. `python -m pytest tests` roda os testes (também no Linux).

## Estrutura

```
src/screencam/
  __main__.py        CLI: console | service-run | agent | install-service | ...
  app.py             ServerCore (serviço) + CaptureAgent (captura) + ConsoleApp
  service.py         serviço Windows (pywin32) + ServiceRunner + SessionManager
  sessions.py        gerência de sessões (lógica) + backend WTS do Windows
  agent.py           entrada do agente de captura por sessão
  control.py         canal de controle loopback serviço↔agentes (atividade)
  activity.py        detecção de mouse/teclado (GetLastInputInfo)
  audio.py           captura WASAPI loopback + microfone e mixagem
  streamer.py        FFmpeg (captura/encode) e MediaMTX (RTSP), com watchdog
  encoders.py        H.265/H.264, detecção de GPU e parâmetros de compressão
  recorder.py        gravação local + retenção
  snapshot.py        JPEG para GetSnapshotUri
  winapi.py          ctypes do Windows (DPI, monitores, inatividade, mutex)
  tray.py            ícone opcional na bandeja (indicador discreto)
  onvif/server.py    serviços Device, Media, Media2, Events (SOAP 1.2)
  onvif/events.py    PullPoint / Notify, eventos de movimento
  onvif/discovery.py WS-Discovery
  onvif/auth.py      WS-UsernameToken e HTTP Digest/Basic
installer/           Inno Setup (instala serviço + trava ACL) + modelo de config
scripts/             build.ps1, launcher, ícone
tests/               testes de integração ONVIF + controle/sessões/serviço
```

## Limitações conhecidas

- **Tela bloqueada / logon / UAC**: o Windows não permite capturar a área de trabalho
  segura; o vídeo para durante o bloqueio e na tela de login (sem usuário, nada a gravar)
  e volta sozinho ao desbloquear.
- **RDP simultâneo**: com `all_sessions`, as sessões publicam no mesmo caminho RTSP, então
  o NVR vê uma por vez. Vários canais ONVIF simultâneos estão na lista de melhorias.
- **Áudio de chamadas com fone**: o loopback captura o dispositivo de saída **padrão**;
  se o usuário trocar de dispositivo, a captura se reajusta na próxima reconexão.
- **Não testado ainda** em hardware Windows real, com GPU e com NVR Intelbras/Hikvision
  físico: os testes aqui cobrem a lógica (ONVIF, controle, sessões) e o pipeline FFmpeg.

## LGPD e uso responsável (importante)

Esta versão grava de forma **discreta** (sem ícone, como serviço), o que **aumenta** o dever
de transparência, não o contrário. A gravação de tela, áudio e microfone de colaboradores
trata dados pessoais e exige base legal. Recomenda-se:

- **Ciência prévia e por escrito** do colaborador (política interna / contrato), mesmo sem
  indicador visível na tela. Considere um **aviso no logon** informando que a sessão é gravada.
- **Finalidade definida e proporcional**, retenção limitada no NVR e acesso restrito às gravações.
- **Microfone**: pode captar conversas de terceiros não relacionados; avalie `microphone = false`
  ou ciente dos presentes.
- O indicador discreto pode ser ligado com `visible_indicator = true` quando a política exigir
  sinalização visível.

A ocultação atende ao requisito operacional (o usuário não desliga a gravação), mas a
conformidade depende dessas medidas organizacionais — não deixe de tratá-las.

## Melhorias sugeridas (próximas versões)

1. **Senha protegida via DPAPI** (hoje o `config.ini` fica com ACL só de administradores;
   o próximo passo é cifrar a senha do NVR na máquina).
2. **Vários canais ONVIF** para RDP simultâneo (uma câmera por sessão no NVR).
3. **Sub-stream** (segundo perfil em baixa resolução) para visualização em mosaico no NVR.
4. **Interface gráfica de configuração** em vez de editar o `config.ini`.
5. **Desktop Duplication (ddagrab)** com a captura direto na GPU (sem cópia para a CPU), reduzindo ainda mais o uso de CPU com H.265.
6. **Pré-buffer local** para não perder os primeiros segundos caso o NVR não tenha pré-gravação.
7. **GOP adaptativo** ("H.265+"): quadro-chave longo enquanto a tela está parada e curto quando há movimento.
8. **Máscara de privacidade** (borrar janelas de bancos, senhas, etc.).
9. **Assinatura digital do instalador** para evitar alertas do SmartScreen/antivírus.
