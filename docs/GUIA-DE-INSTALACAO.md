# ScreenCam ONVIF — Guia de instalação e solução de problemas

Versão do guia: 1.6.0 · Público: técnico que instala o programa em cada computador.

O ScreenCam ONVIF faz o computador aparecer para o gravador (NVR) como uma **câmera IP ONVIF**:
a tela e o áudio vão para o NVR, que grava quando há atividade (mouse/teclado). Roda como
**serviço do Windows** (o usuário comum não consegue fechar) e grava **todos os usuários** do PC.

> **Ideia central:** a câmera é **passiva**. É o **NVR que conecta no PC**, igual a uma câmera
> IP comum. Por isso o PC precisa ser alcançável pelo NVR (mesma rede ou rota).

---

## 1. Antes de começar — reúna estes dados

| Dado | Exemplo | Observação |
|---|---|---|
| IP do NVR | `192.168.25.200` | Confira com cuidado: erro de digitação aqui bloqueia o NVR no firewall |
| Usuário da câmera | `admin` | É o usuário que o **NVR** vai digitar ao cadastrar este PC |
| Senha da câmera | 10 caracteres | O gravador exige **10**. Não use `;` nem `#` |
| Porta ONVIF | `80` (igual às outras câmeras) ou `8000` | Se a porta estiver ocupada o serviço escolhe outra e avisa (ver 7.2) |
| Acesso | Administrador do Windows | Para instalar e para o serviço |

Requisitos do PC: Windows 10/11 64 bits, rede com acesso ao NVR (ping), relógio com **hora automática**.

---

## 2. Instalação (assistente)

1. Copie `ScreenCamONVIF-Setup-x.y.z.exe` para o PC e execute **como Administrador**
   (botão direito → *Executar como administrador*).
2. Tela **Configuração da câmera ONVIF**:
   - *Nome do dispositivo*: deixe `ScreenCam-{computer}` (usa o nome do PC).
   - *Usuário* e *Senha* (10 caracteres) + *Confirmar senha*.
   - *IP do NVR*: preencha o IP do gravador. O firewall e o acesso ao vídeo ficam liberados **só para ele**.
     Em branco = libera a sub-rede local inteira.
   - *Porta ONVIF*: a que o NVR usa para câmeras ONVIF (normalmente `80`; alternativa `8000`).
   - *Porta RTSP*: deixe `8554`.
3. Tela **Registro ativo no NVR**: **deixe a porta em branco** (recurso experimental, só para casos de NAT).
4. Mantenha marcado *Liberar o programa no Firewall*.
5. Ao final o instalador **instala e inicia o serviço**. Se o serviço não subir, aparece um aviso
   com o caminho para o reparo (seção 6).

### Instalação silenciosa (sem telas) — para várias máquinas

Em um Prompt/PowerShell **como administrador**, na pasta do instalador:

```
.\ScreenCamONVIF-Setup-1.6.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=C:\instalacao.log /NVRIP=192.168.25.200 /USER=admin /PASS=<10-caracteres> /ONVIFPORT=80
```

| Parâmetro | Função | Padrão |
|---|---|---|
| `/NVRIP=` | IP do NVR (restringe firewall e leitura) | vazio |
| `/USER=` `/PASS=` | Credenciais que o NVR usará (senha de 10 caracteres) | `admin` / **obrigatória** |
| `/ONVIFPORT=` `/RTSPPORT=` | Portas | `8000` / `8554` |
| `/NAME=` | Nome do dispositivo | `ScreenCam-{computer}` |
| `/FORCECONFIG=1` | **Regrava** o `config.ini` mesmo se já existir (reinstalar com novos dados) | não |

Sem `/FORCECONFIG=1`, uma **atualização preserva** a configuração existente.

---

## 3. Cadastro no NVR (Hikvision)

No NVR: **Câmera → Gerenciamento de câmera → Adicionar/Adição personalizada** (o nome do menu varia com
o firmware; o que importa são os campos abaixo).

| Campo no NVR | Valor |
|---|---|
| Endereço IP da câmera | **IP deste PC** (rode `ipconfig` no PC) |
| Protocolo | **ONVIF** |
| Porta de gerenciamento | A **porta ONVIF** (a que `ScreenCamONVIF.exe status` mostra) |
| Usuário / Senha | Os definidos na instalação (10 caracteres) |
| Protocolo de transferência | Automático (ou TCP) |

Depois: habilite a **detecção de movimento/evento ONVIF** no canal e, na agenda de gravação, use
**Evento/Movimento**. Deixe o stream em **H.265** e ative o áudio.

Pontos de atenção do Hikvision:
- **Não dependa da busca automática** (SADP/Quick Add): ela costuma listar só equipamentos Hikvision e não cruza sub-redes. Cadastre **manualmente por IP**.
- **Data/hora** do PC e do NVR devem ser iguais (o ONVIF recusa o login se o relógio divergir).
- A "porta de comunicação 8000" do NVR é a porta do próprio gravador; **não** é a porta deste PC.

Intelbras: *Adicionar dispositivo → manual*, fabricante **ONVIF**, IP do PC, porta ONVIF, usuário/senha.

---

## 4. Como conferir se está funcionando

Em um Prompt **como administrador** (na pasta `C:\Program Files\ScreenCamONVIF`):

| Comando | Mostra |
|---|---|
| `ScreenCamONVIF.exe status` | Se o serviço roda, **a porta ONVIF efetiva**, RTSP, codec e o NVR autorizado |
| `ScreenCamONVIF.exe doctor` | Diagnóstico: config, senha, ffmpeg, portas (e quem as usa), rede até o NVR, serviço |
| `powershell -ExecutionPolicy Bypass -File tools\verificar-instalacao.ps1 -User admin -Password <senha> -RequireStream` | Teste completo: serviço, portas, ONVIF autenticado, Media2/H.265, RTSP, **vídeo+áudio publicados**, logs |

Resultado esperado do verificador: `RESULTADO: tudo funcionando`, com a linha
`Stream publicado pelo agente de captura: video + audio (H.265)`.

> O vídeo só existe quando **há um usuário conectado** na área de trabalho (o programa grava a sessão ativa).
> Em tela bloqueada ou sem ninguém logado não há imagem para capturar.

---

## 5. Onde ficam os arquivos

| O quê | Onde |
|---|---|
| Programa | `C:\Program Files\ScreenCamONVIF` |
| Configuração (senha cifrada) | `C:\ProgramData\ScreenCamONVIF\config.ini` — só administradores leem |
| Log do serviço | `C:\ProgramData\ScreenCamONVIF\logs\service.log` |
| Log do agente de captura | `C:\ProgramData\ScreenCamONVIF\logs\agente-sessao-N.log` |
| Portas efetivas | `C:\ProgramData\ScreenCamONVIF\runtime\status.json` |
| Relatórios de reparo | `C:\ProgramData\ScreenCamONVIF\logs\reparar-*.txt` |

Para mudar a configuração: edite o `config.ini` (como admin) e rode `ScreenCamONVIF.exe stop` e depois `start`.

---

## 6. Reparo automático (quando algo não funciona)

Menu Iniciar → **ScreenCam ONVIF → Reparar e diagnosticar (admin)**, ou:

```
powershell -ExecutionPolicy Bypass -File "C:\Program Files\ScreenCamONVIF\tools\reparar.ps1" -NvrIp 192.168.25.200 -OnvifPort 80
```

Pede a senha no terminal (não fica gravada no relatório) e faz: diagnóstico → verifica a porta →
regrava o `config.ini` → recria o firewall (só para o NVR) → confere o relógio → reinstala e inicia o
serviço (se não subir, roda em primeiro plano e mostra o erro real) → verificação de ponta a ponta.
No fim imprime os valores para o cadastro no NVR e grava um relatório. **Envie o relatório ao suporte** se persistir.

---

## 7. Problemas comuns

### 7.1 O serviço não inicia
Rode `ScreenCamONVIF.exe start`: ele espera o serviço subir e, se falhar, mostra o **motivo** e o fim do `service.log`.
Depois `doctor`. Causas já encontradas:

| Sintoma no log | Causa | Solução |
|---|---|---|
| `WinError 10013` ao abrir a porta ONVIF | Porta ocupada (ex.: **IIS/http.sys** na 80) | Versões ≥ 1.6 escolhem outra porta sozinhas. Veja a efetiva em `status` e use-a no NVR. Para usar a 80: `iisreset /stop` (ou pare o programa que a usa) e reinicie o serviço |
| `ZeroDivisionError` ao iniciar | Serviço (sessão 0) sem monitor | Corrigido na 1.6 |
| Serviço “não existe” | Instalação do serviço falhou | `ScreenCamONVIF.exe install-service` e `start`, ou `reparar.ps1` |

### 7.2 Porta ONVIF 80 ocupada
Normal em PCs com IIS/WinRM/SQL Reporting. O serviço usa `8000` (ou `8080`, `8899`, `8001`) e **avisa**:
`ScreenCamONVIF.exe status` → *“cadastre a câmera no NVR com Porta = 8000”*. Cadastre o NVR com essa porta.

### 7.3 O NVR não encontra / não adiciona a câmera
1. Cadastre **manualmente por IP** (seção 3); não dependa da busca automática.
2. Do PC: `ping <IP do NVR>`. Do NVR: alcance `<IP do PC>:<porta ONVIF>`.
3. `ScreenCamONVIF.exe doctor` avisa se o **IP do NVR está em outra sub-rede** (erro de digitação típico:
   `192.168.26.x` em vez de `192.168.25.x`). Corrija `nvr_ip` no `config.ini` (ou deixe vazio) e reinicie o serviço.
4. Firewall: as regras *ScreenCam ONVIF* e *ScreenCam ONVIF RTSP* só liberam o IP do NVR.
5. Antivírus/EDR de terceiros podem bloquear; libere `C:\Program Files\ScreenCamONVIF`.

### 7.4 Cadastra, mas pede senha / “falha de autenticação”
- Senha **com 10 caracteres** e igual à do `config.ini`; sem `;` ou `#`.
- **Relógio** do PC e do NVR diferentes (resincronize: `w32tm /resync /force`).
- `doctor` mostra *“Senha … cifrada mas não foi possível decifrar”* se o `config.ini` foi copiado de outro PC
  (a cifra é presa à máquina): regrave a senha em texto e rode `ScreenCamONVIF.exe protect-config`.

### 7.5 Cadastra mas **sem imagem**
- Há alguém **logado** (e a tela não está bloqueada)? O vídeo é da sessão ativa.
- Verificador com `-RequireStream` e os logs `agente-sessao-N.log` mostram o erro do FFmpeg.
- Se o NVR mostra preto/“codec não suportado”: no `config.ini`, `[video] codec = h264`, reinicie o serviço.

### 7.6 Sem áudio
Precisa de **dispositivo de áudio** (alto-falante para o som do sistema; microfone para a voz). Sem eles o
programa envia silêncio (log: `Falha na captura de audio ... 0x80070490` = dispositivo não encontrado). Se o NVR
não toca o áudio, tente `[audio] codec = g711`.

### 7.7 Consumo de CPU alto
H.265 por software (x265) pesa. Em PC com GPU use `[video] encoder = auto` (detecta NVIDIA/Intel/AMD) e/ou
reduza `fps` e `max_width/max_height`.

---

## 8. Desinstalar

*Configurações → Aplicativos → ScreenCam ONVIF → Desinstalar* (como admin), ou
`C:\Program Files\ScreenCamONVIF\unins000.exe /VERYSILENT`. Remove o serviço, o firewall e para o mediamtx.
As gravações e logs em `C:\ProgramData\ScreenCamONVIF` permanecem.

---

## 9. Checklist de instalação (imprima e preencha)

- [ ] Hora automática ativada no Windows; `ping` do PC ao NVR responde
- [ ] IP do NVR anotado: ______________  (conferido: mesma sub-rede?)
- [ ] Usuário: __________  Senha (10 caracteres): ____________ (guardar em local seguro)
- [ ] Porta ONVIF desejada: ______  Porta efetiva (`status`): ______
- [ ] Instalador executado como **administrador**
- [ ] `ScreenCamONVIF.exe status` → “em execução”
- [ ] `verificar-instalacao.ps1 ... -RequireStream` → “tudo funcionando”
- [ ] Câmera cadastrada **manualmente** no NVR (IP do PC, ONVIF, porta, usuário/senha)
- [ ] Gravação por **movimento/evento** ativada no canal; H.265 e áudio habilitados
- [ ] Imagem e áudio ao vivo conferidos no NVR; movimento do mouse gera evento
- [ ] **Aviso/ciência dos usuários** sobre a gravação (LGPD) registrado

---

## 10. O que foi testado (registro técnico)

Cada versão passa por uma esteira automática (GitHub Actions) com **Windows real** e administrador:
instala o `.exe` em modo silencioso → inicia o serviço → valida ONVIF com autenticação WS-Security
(senha correta aceita e **errada recusada**), Media1/Media2 (H.265), RTSP, **vídeo+áudio publicados pelo agente** na sessão do
usuário → atualiza por cima → roda o `reparar.ps1` → desinstala e confere a limpeza.

Problemas encontrados por esses testes e corrigidos na 1.6.0: serviço morria sem monitor (divisão por zero);
serviço morria com porta ocupada pelo IIS; instalador travava no modo silencioso; FFmpeg novo recusava
`-thread_queue_size` (câmera cadastrava, sem vídeo); agente em laço de reinício sem diagnóstico.

**Ainda não validado:** cadastro em um NVR Hikvision/Intelbras **físico** (a esteira não tem gravador), e
captura com GPU/placa de áudio reais. Ao instalar no primeiro NVR real, registre aqui o modelo/firmware e o resultado.

| Data | NVR (modelo/firmware) | Resultado | Observações |
|---|---|---|---|
|  |  |  |  |

---

## 11. Segurança e LGPD (resumo)

- A senha fica **cifrada** (DPAPI, presa à máquina) e o `config.ini` só abre para administradores.
- O acesso ao vídeo é liberado apenas ao IP do NVR (firewall + leitura RTSP).
- O programa detecta **que** houve atividade, **não registra o que é digitado**.
- Gravar tela/áudio de pessoas exige **base legal, ciência prévia por escrito, finalidade e retenção definidas** e
  acesso restrito às gravações. O microfone pode captar terceiros: avalie `[audio] microphone = false`.
