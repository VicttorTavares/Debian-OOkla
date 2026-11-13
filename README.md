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
 │                  ├─► FFmpeg (H.264 + AAC/G.711) ─► MediaMTX (RTSP) ◄──┼── NVR (porta 8554)
 │  Áudio WASAPI  ──┘        loopback + microfone                        │
 │                                                    WS-Discovery ◄─────┼── busca do NVR (UDP 3702)
 └───────────────────────────────────────────────────────────────────────┘
```

## Funcionalidades

| Recurso | Detalhe |
|---|---|
| Câmera ONVIF (Profile S) | Device, Media e Events. Testado com cliente ONVIF real (`onvif-zeep-async`, usado pelo Home Assistant) |
| Descoberta automática | WS-Discovery: o NVR encontra o PC em "Buscar dispositivos" |
| Vídeo | H.264 Main, até 1920×1080 (configurável), 15 fps, GOP de 2 s. Suporte a NVENC/QuickSync/AMF |
| Áudio | Som do sistema (loopback) + microfone, mixados. AAC 48 kHz ou G.711 µ-law 8 kHz |
| Gatilho por atividade | 1º movimento do mouse ou tecla → `IsMotion=true`. Após N s ocioso → `IsMotion=false` |
| Eventos | `tns1:RuleEngine/CellMotionDetector/Motion` e `tns1:VideoSource/MotionAlarm` via PullPoint e Notify (push) |
| Snapshot | `GetSnapshotUri` (JPEG) para miniaturas no NVR |
| Segurança | WS-UsernameToken (digest), HTTP Digest/Basic; RTSP com usuário/senha |
| Carimbo no vídeo | Computador, usuário, data e hora sobre a imagem (útil como prova) |
| Gravação local (opcional) | Backup em MKV somente durante a atividade, com retenção por dias/espaço |
| Privacidade | Ícone visível na bandeja; **não registra o que é digitado** (só detecta que houve entrada) |

## Instalação (usuário final)

1. Execute `ScreenCamONVIF-Setup-1.0.0.exe` como administrador.
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
3. **Áudio**: habilite o áudio no canal (*Stream principal → Tipo: Vídeo e Áudio*).
   Se o NVR não reproduzir o áudio, altere `codec = g711` no `config.ini`.
4. Em alguns NVRs é preciso ativar *"Detecção de movimento"* / *"Eventos ONVIF"* no canal
   (Intelbras, Hikvision, Dahua e similares aceitam os tópicos `CellMotionDetector` ou `MotionAlarm`).

## Configuração (`config.ini`)

Principais opções (o arquivo completo é comentado):

| Seção | Chave | Padrão | Descrição |
|---|---|---|---|
| `[video]` | `monitor` | `all` | `all` ou número do monitor (1 = principal) |
| | `fps` / `bitrate_kbps` | `15` / `2000` | Taxa de quadros e de bits |
| | `encoder` | `libx264` | `h264_nvenc`, `h264_qsv`, `h264_amf` para usar a GPU |
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
# => installer\Output\ScreenCamONVIF-Setup-1.0.0.exe
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
  recorder.py        gravação local + retenção
  snapshot.py        JPEG para GetSnapshotUri
  tray.py            ícone e menu na bandeja
  onvif/server.py    serviços Device, Media, Events (SOAP 1.2)
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
4. **Desktop Duplication (ddagrab)** com encoder de GPU para reduzir o uso de CPU em 4K/multimonitor.
5. **Pré-buffer local** para não perder os primeiros segundos caso o NVR não tenha pré-gravação.
6. **Media2 / H.265** para NVRs mais novos e economia de disco.
7. **Máscara de privacidade** (borrar janelas de bancos, senhas, etc.).
8. **Assinatura digital do instalador** para evitar alertas do SmartScreen/antivírus.
