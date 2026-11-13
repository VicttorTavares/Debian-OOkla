# ScreenCam ONVIF

Software para Windows que transforma o computador em uma **câmera IP ONVIF**:
a tela (com **áudio** do sistema e do microfone) é transmitida por RTSP, e cada
**movimento do mouse ou tecla pressionada** gera um evento de **"detecção de
movimento"** para o NVR. Com isso, o NVR grava exatamente os períodos em que o
usuário está usando o computador, como faz com uma câmera de verdade.

```
 ┌───────────────────────── Computador Windows ──────────────────────────┐
 │                                                                       │
 │  GetLastInputInfo ──► ActivityMonitor ──► evento "Motion" ─┐          │
 │  (mouse/teclado)                                           │          │
 │                                                            ▼          │
 │  Tela (gdigrab) ─┐                                 Servidor ONVIF ◄───┼── NVR (porta 8000)
 │                  ├─► FFmpeg (H.265 + AAC/G.711) ─► MediaMTX (RTSP) ◄──┼── NVR (porta 8554)
 │  Áudio WASAPI  ──┘        loopback + microfone                        │
 │                                                    WS-Discovery ◄─────┼── busca do NVR (UDP 3702)
 └───────────────────────────────────────────────────────────────────────┘
```

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
| Privacidade | Ícone visível na bandeja; **não registra o que é digitado** (só detecta que houve entrada) |

## Instalação (usuário final)

1. Execute `ScreenCamONVIF-Setup-1.1.0.exe` como administrador.
2. No assistente, informe **nome do dispositivo, usuário, senha, porta ONVIF e porta RTSP**.
3. Deixe marcadas as opções *Iniciar automaticamente* e *Liberar no Firewall*.
4. O ícone de câmera aparece na bandeja: **cinza** = ocioso, **vermelho** = usuário ativo
   (evento de movimento enviado), **amarelo** = pausado.

A configuração fica em `C:\ProgramData\ScreenCamONVIF\config.ini` e os logs em
`%LOCALAPPDATA%\ScreenCamONVIF\logs`.

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
5. Em alguns NVRs é preciso ativar *"Detecção de movimento"* / *"Eventos ONVIF"* no canal
   (Intelbras, Hikvision, Dahua e similares aceitam os tópicos `CellMotionDetector` ou `MotionAlarm`).

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

## Gerar o instalador (desenvolvedor)

No Windows 10/11 com Python 3.11+:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
# => installer\Output\ScreenCamONVIF-Setup-1.1.0.exe
```

O script baixa o **FFmpeg** (gyan.dev) e o **MediaMTX v1.21.3**, roda os testes, gera o
executável com PyInstaller e compila o instalador com Inno Setup. O workflow
`.github/workflows/build-windows.yml` faz o mesmo no GitHub Actions e publica o
instalador como artefato.

Para executar a partir do código-fonte: `pip install -r requirements.txt`, coloque
`ffmpeg.exe` e `mediamtx.exe` em `vendor\` e rode `python -m screencam` dentro de `src\`.
`python -m pytest tests` roda os testes (também no Linux).

## Estrutura

```
src/screencam/
  __main__.py        ponto de entrada (CLI / bandeja)
  app.py             orquestra todos os componentes
  activity.py        detecção de mouse/teclado (GetLastInputInfo)
  audio.py           captura WASAPI loopback + microfone e mixagem
  streamer.py        FFmpeg (captura/encode) e MediaMTX (RTSP), com watchdog
  encoders.py        H.265/H.264, detecção de GPU e parâmetros de compressão
  recorder.py        gravação local + retenção
  snapshot.py        JPEG para GetSnapshotUri
  tray.py            ícone e menu na bandeja
  onvif/server.py    serviços Device, Media, Media2, Events (SOAP 1.2)
  onvif/events.py    PullPoint / Notify, eventos de movimento
  onvif/discovery.py WS-Discovery
  onvif/auth.py      WS-UsernameToken e HTTP Digest/Basic
installer/           Inno Setup + modelo de config
scripts/             build.ps1, launcher, ícone
tests/               testes de integração ONVIF
```

## Limitações conhecidas

- **Tela bloqueada / UAC**: o Windows não permite capturar a área de trabalho segura;
  o vídeo para durante o bloqueio e volta sozinho ao desbloquear.
- **Uma sessão por computador**: as portas são fixas, então em servidores com vários
  usuários simultâneos (RDS/Terminal Server) apenas uma sessão transmite.
- **Áudio de chamadas com fone**: o loopback captura o dispositivo de saída **padrão**;
  se o usuário trocar de dispositivo, a captura se reajusta na próxima reconexão.
- O `config.ini` (com a senha do NVR) é legível pelos usuários locais, pois o
  programa roda na sessão do usuário. Veja a sugestão de serviço abaixo.

## LGPD e uso responsável

Gravar a tela e o áudio de colaboradores envolve dados pessoais. Recomenda-se:
política interna escrita e ciência do colaborador, finalidade definida, retenção
limitada no NVR, acesso restrito às gravações e cuidado com o microfone (que pode
captar conversas de terceiros — avalie deixar `microphone = false`). O software
mantém um ícone visível na bandeja e não registra o conteúdo digitado.

## Melhorias sugeridas (próximas versões)

1. **Serviço do Windows + agente por sessão**: ONVIF/RTSP rodando como serviço
   (SYSTEM, senha protegida via DPAPI) e um agente leve na sessão do usuário só
   para capturar. Impede que o usuário encerre o processo ou leia a senha.
2. **Sub-stream** (segundo perfil em baixa resolução) para visualização em mosaico no NVR.
3. **Interface gráfica de configuração** em vez de editar o `config.ini`.
4. **Desktop Duplication (ddagrab)** com a captura direto na GPU (sem cópia para a CPU), reduzindo ainda mais o uso de CPU com H.265.
5. **Pré-buffer local** para não perder os primeiros segundos caso o NVR não tenha pré-gravação.
6. **GOP adaptativo** ("H.265+"): quadro-chave longo enquanto a tela está parada e curto quando há movimento.
7. **Máscara de privacidade** (borrar janelas de bancos, senhas, etc.).
8. **Assinatura digital do instalador** para evitar alertas do SmartScreen/antivírus.
