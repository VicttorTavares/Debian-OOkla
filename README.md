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
| Senha do NVR | Cifrada no disco com **DPAPI** (escopo de máquina) + `config.ini` só para administradores |
| Carimbo no vídeo | Computador, usuário, data e hora sobre a imagem (útil como prova) |
| Gravação local (opcional) | Backup em MKV somente durante a atividade, com retenção por dias/espaço |
| Serviço do Windows | Roda como SYSTEM, inicia no boot, reinicia sozinho; **o usuário comum não fecha** |
| Multiusuário | Canal único que segue a sessão ativa; troca de usuário e RDP gravam no mesmo canal |
| Discrição | Sem janela/ícone por padrão (`visible_indicator = false`); **não registra o que é digitado** |

> **Instalando em outra máquina?** Siga o [Guia de instalação e solução de problemas](docs/GUIA-DE-INSTALACAO.md) (passo a passo, cadastro no NVR, reparo automático e checklist).

## Instalação (administrador)

1. Execute `ScreenCamONVIF-Setup-1.6.0.exe` como administrador.
2. No assistente, informe:
   - **nome do dispositivo, usuário e senha** (a senha deve ter **10 caracteres** — exigência
     de gravadores Intelbras/Hikvision —, com confirmação);
   - **IP do NVR** (opcional): se preenchido, o dispositivo passa a **pertencer àquele gravador**
     — só ele consegue ler o stream (firewall + usuário de leitura restritos a esse IP);
   - **porta ONVIF** (informe a mesma no campo *Porta de gerenciamento* do NVR) e **porta RTSP**.
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
ScreenCamONVIF.exe protect-config    cifrar a senha do NVR no config.ini (DPAPI)
ScreenCamONVIF.exe uninstall-service remover o serviço
ScreenCamONVIF.exe console           rodar em primeiro plano, 1 usuário (teste/diagnóstico)
ScreenCamONVIF.exe service-console   rodar o serviço + agentes em primeiro plano (diagnóstico)
```

O serviço é protegido pelo Windows: parar/remover exige privilégio de administrador.
Um usuário comum que encerre o agente de captura pelo Gerenciador de Tarefas o verá
reiniciar em poucos segundos.

### Senha do NVR protegida (DPAPI)

A senha do NVR **não fica em texto claro** no disco. Na instalação ela é cifrada com a
**DPAPI** do Windows (escopo de máquina) e gravada como `password = enc:v1:...`. Como o
serviço roda como SYSTEM e o instalador como administrador — contas diferentes —, o escopo
é o de **máquina**; somado à ACL (o `config.ini` só abre para administradores), isso garante:

- a senha não aparece legível no arquivo;
- se alguém **copiar o `config.ini` para outro computador**, a senha **não pode ser
  decifrada** (a chave DPAPI é vinculada àquela máquina) — o programa trata isso com falha
  segura (senha vazia, sem vazamento).

Para **trocar a senha**: escreva a nova em texto claro no `config.ini` e rode, como admin,
`ScreenCamONVIF.exe stop && ScreenCamONVIF.exe protect-config && ScreenCamONVIF.exe start`.
O serviço também re-cifra sozinho qualquer senha deixada em texto ao iniciar.

## Configuração no NVR

> **Como funciona (modelo ONVIF):** este programa é uma **câmera passiva** — é o **NVR que se
> conecta a ele** (igual a cadastrar uma câmera IP no gravador: você digita o IP da câmera no
> NVR). O computador não "aponta" para o NVR; ele apenas atende. O campo **IP do NVR** do
> instalador serve para **vincular** o dispositivo a um gravador específico: quando preenchido,
> só aquele IP consegue puxar o vídeo (firewall + leitura RTSP/ONVIF restritos a ele).

1. **Adicionar câmera**: protocolo **ONVIF**, **IP do computador** (a "câmera"), porta de
   gerenciamento = **porta ONVIF** definida na instalação (padrão **8000**), usuário/senha
   definidos na instalação (ou use a busca automática). A senha tem **10 caracteres**.
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
- **Hikvision** (DS-7xxx/iDS, Hik-Connect): *Configuração → Sistema → Gerenciamento de câmera
  → Adição personalizada (Custom Add)* → *Protocolo: ONVIF*, **IP do PC**, porta **8000**,
  usuário/senha da instalação. **Não** dependa do *Quick Add*/SADP (busca por broadcast, que
  não cruza sub-redes). O Hikvision lê o codec pelo Media2 (H.265 ok). Ative *Evento → Evento
  Básico → Detecção de Movimento* no canal e, na agenda, selecione *Evento/Movimento*.
  Cuidados típicos do Hikvision:
  - **Data/hora** do PC e do NVR precisam bater (o ONVIF digest falha se o relógio divergir).
  - **Senha** forte (daí a exigência de 10 caracteres); o usuário ONVIF precisa de permissão de evento.
  - O Hikvision **não** usa a tela "End. servidor / ID dispositivo" (isso é Intelbras/Dahua).
    Para câmera atrás de NAT, o equivalente Hikvision é o **ISUP/EHome** (registro numa
    plataforma), um protocolo diferente e mais complexo — o "registro ativo" experimental deste
    programa **não** fala ISUP. Entre sub-redes **roteadas**, use a adição manual por IP acima.

> Em ambos, se o NVR só gravar "contínuo", ele ainda grava — mas sem a economia do gatilho
> por movimento. Vale conferir se o canal está em modo *Evento*.

## O NVR não encontra a câmera na busca automática

A busca automática (WS-Discovery) usa **multicast**, que **não cruza sub-redes/VLANs**. Se o
NVR está em `192.168.25.x` e o PC em `192.168.26.x`, a busca **não acha** a câmera — isso é
esperado. Faça assim, na ordem:

1. **Cadastre manualmente** no NVR (funciona entre sub-redes, desde que haja roteamento):
   protocolo **ONVIF**, **IP do computador**, porta de gerenciamento = **porta ONVIF** (8000),
   usuário/senha da instalação. É a solução mais simples e confiável.
2. **Informe o IP do NVR** na instalação (ou em `[network] nvr_ip`). Sem ele, o firewall é
   liberado só para a **sub-rede local** (`localsubnet`) e **bloquearia** um NVR de outra
   sub-rede. Com o IP preenchido, o firewall e a leitura RTSP/ONVIF são liberados para o NVR.
3. **Teste a conectividade**: do NVR, veja se alcança `IP-DO-PC:8000`; do PC, `ping IP-DO-NVR`.
   Se não houver rota entre as sub-redes, nem o cadastro manual funciona — aí use o passo a seguir.

### Registro ativo no NVR (experimental)

Quando **não há rota** do NVR até o PC (NAT, sub-redes isoladas), o modelo passivo não serve.
Nesse caso o dispositivo pode **conectar no NVR** — é a tela *End. servidor / ID dispositivo*
das câmeras Intelbras/Dahua. A instalação tem a página **"Registro ativo no NVR"**; preencha a
**porta do Registro Automático** do NVR (deixe em branco para não usar), o **ID do dispositivo**
e, se exigido, **usuário/senha do NVR**. No `config.ini` isso fica em `[autoregister]`.

O programa então conecta ao NVR e abre um **túnel** até o serviço ONVIF local. **É experimental:**
o enquadramento exato do handshake varia por modelo de NVR; o transporte (conectar, reconectar,
repassar os dados) está implementado e testado, mas pode ser preciso ajustar o preâmbulo para o
seu gravador. Se precisar, uma **captura de pacotes** (Wireshark) de uma câmera Intelbras real
registrando-se no seu NVR permite acertar o formato — me envie que eu ajusto.

> **Hikvision:** esse modelo é do Intelbras/Dahua. O Hikvision não tem "End. servidor"; para
> câmera atrás de NAT ele usa **ISUP/EHome** (registro em plataforma), não suportado aqui. Com
> Hikvision, prefira a **adição manual por IP** (porta 8000) quando as redes forem roteadas.

> Dica de diagnóstico: `ScreenCamONVIF.exe status` mostra o NVR autorizado e o estado do
> registro ativo; os logs em `C:\ProgramData\ScreenCamONVIF\logs` registram cada tentativa.

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
| | `nvr_ip` | *(vazio)* | IP do gravador; vazio = qualquer NVR da rede. Preenchido, restringe o acesso a ele |
| | `onvif_port` / `rtsp_port` | `8000` / `8554` | Portas; informe a ONVIF como "gerenciamento" no NVR |
| `[service]` | `mode` | `service` | `service` (SYSTEM + agente) ou `console` (1 processo) |
| | `capture_scope` | `active_console` | Canal único; segue a sessão ativa |
| | `visible_indicator` | `false` | Ícone discreto na bandeja do usuário |
| `[autoregister]` | `enabled` | `false` | Registro ativo no NVR (experimental; ver acima) |
| | `server_ip` / `server_port` | *(vazio)* / `7000` | NVR e porta do Registro Automático |
| | `device_id` | `1` | ID do dispositivo no NVR |

## Multiusuário e sessões

O requisito "todos os usuários da máquina são gravados" é atendido pelo **gerenciador de
sessões** do serviço, com **um canal único compartilhado** (uma câmera por PC no NVR):

- Em qualquer momento, o serviço mantém **um** agente capturando, na **sessão ativa**.
- Em PCs compartilhados com **troca de usuário** (ou logins sequenciais), o agente segue a
  sessão que ficou ativa — João sai, Maria entra, a gravação passa para a Maria.
- Com **várias sessões RDP simultâneas**, o canal único segue a sessão ativa (console tem
  prioridade; entre sessões RDP, a de menor ID). Todos os usuários acabam gravados **no mesmo
  canal**, um de cada vez — que foi o comportamento escolhido. `active_console` e
  `all_sessions` levam, hoje, a esse mesmo canal único.

O serviço injeta o agente via `WTSQueryUserToken` + `CreateProcessAsUser` (área de trabalho
`winsta0\default`) e o reinicia se cair. O agente só recebe uma configuração **sem senhas**
(`ProgramData\ScreenCamONVIF\runtime\agent.json`); as credenciais do NVR ficam só no serviço.

## Gerar o instalador (desenvolvedor)

No Windows 10/11 com Python 3.11+:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
# => installer\Output\ScreenCamONVIF-Setup-1.6.0.exe
```

O script baixa o **FFmpeg** (GitHub/BtbN, com gyan.dev como alternativa, repetição e
cache no CI) e o **MediaMTX v1.21.3**, roda os testes, gera o
executável com PyInstaller (aplicativo de **console**, para os comandos de admin imprimirem;
o serviço e o agente não mostram janela) e compila o instalador com Inno Setup. O workflow
`.github/workflows/build-windows.yml` faz o mesmo no GitHub Actions e publica o
instalador como artefato — a forma mais simples de obter o `.exe` sem uma máquina Windows à mão.

> O empacotamento inclui `--collect-submodules screencam` (módulos de carga tardia) e os
> *hidden imports* do pywin32 (serviço). Caso o Inno Setup não esteja instalado, o script o
> instala via winget/choco. Requer PowerShell como administrador apenas se for testar o
> serviço na mesma máquina.

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
  autoregister.py    registro ativo no NVR (tunel reverso, experimental)
  activity.py        detecção de mouse/teclado (GetLastInputInfo)
  audio.py           captura WASAPI loopback + microfone e mixagem
  streamer.py        FFmpeg (captura/encode) e MediaMTX (RTSP), com watchdog
  encoders.py        H.265/H.264, detecção de GPU e parâmetros de compressão
  recorder.py        gravação local + retenção
  snapshot.py        JPEG para GetSnapshotUri
  winapi.py          ctypes do Windows (DPI, monitores, inatividade, mutex)
  dpapi.py           cifragem das senhas (CryptProtectData, escopo maquina)
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
- **RDP simultâneo**: canal único — o NVR grava a sessão ativa de cada vez, não as várias
  simultaneamente (comportamento escolhido). Vários canais ONVIF é uma possível evolução futura.
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

1. **Sub-stream** (segundo perfil em baixa resolução) para visualização em mosaico no NVR.
2. **Interface gráfica de configuração** em vez de editar o `config.ini`.
3. **Desktop Duplication (ddagrab)** com a captura direto na GPU (sem cópia para a CPU), reduzindo ainda mais o uso de CPU com H.265.
4. **Pré-buffer local** para não perder os primeiros segundos caso o NVR não tenha pré-gravação.
5. **GOP adaptativo** ("H.265+"): quadro-chave longo enquanto a tela está parada e curto quando há movimento.
6. **Máscara de privacidade** (borrar janelas de bancos, senhas, etc.).
7. **Assinatura digital do instalador** para evitar alertas do SmartScreen/antivírus.
