# AI Video Studio

Editor de vídeo que corta seus takes sozinho: você grava falando, aprova o
roteiro, e ele monta a edição escolhendo a melhor tentativa de cada frase,
descartando os erros e as regravações. A legenda sai pronta, sincronizada.

A edição roda com **a sua própria assinatura** de Claude ou ChatGPT — nada passa
por servidor de terceiro, e seus vídeos nunca saem da sua máquina.

> **Antes de instalar, entenda o que você está rodando.** O chat do app não é um
> assistente de texto: ele executa comandos no seu Mac, com a sua permissão já
> concedida de antemão, para conseguir cortar e renderizar os vídeos. É o mesmo
> poder que o Claude Code tem quando você o usa no terminal. Por isso o app
> escuta **só na sua própria máquina** — ninguém na sua rede alcança ele. Se você
> abrir para a rede (para editar pelo celular), o instalador exige um PIN.
>
> Instale isto na sua máquina pessoal, não numa máquina compartilhada ou de
> trabalho.

---

## Instalação

Você precisa de um Mac. Abra o Terminal e cole:

```bash
git clone https://github.com/renatoklimeck/ai-video-studio.git ~/video-studio && cd ~/video-studio && ./install.sh
```

A instalação leva de 10 a 30 minutos, quase tudo download. Ela pede sua senha
uma vez (para o Homebrew) e pergunta se você quer o modelo de transcrição grande
(2,9 GB, melhor qualidade — pode instalar depois).

No fim, o app abre sozinho em `https://localhost:3030`.

### Você também precisa de uma IA logada

O app não traz IA embutida: ele usa a **sua** assinatura. Instale uma das duas e
faça login:

```bash
npm i -g @anthropic-ai/claude-code    # depois rode: claude
npm i -g @openai/codex                # depois rode: codex
```

Se as edições falharem com erro de autenticação, rode `claude setup-token`. O
login normal expira quando o app chama a IA em segundo plano; esse comando gera
uma credencial que dura.

---

## Como usar

1. **Importe** seu vídeo bruto (arraste para a tela inicial).
2. Clique no preset **"First edit — pick takes"**.
3. A IA lê o vídeo e escreve o **roteiro limpo** — a melhor versão de cada frase.
   Um pop-up aparece quando fica pronto.
4. **Revise e aprove** o roteiro. É o seu controle sobre o que entra no vídeo:
   a edição segue essas linhas à risca.
5. Ele monta o corte e gera a legenda. Ajuste o que quiser na timeline.
6. **Export**.

O chat aceita pedidos em português: "aumenta a legenda", "tira o silêncio no
take 3", "troca o take da linha 5".

---

## Atualizações

Quando sair uma versão nova, aparece um botão **↑ Update** no topo do app. Clique
e espere: ele baixa, reconstrói e recarrega a tela sozinho. Não precisa de
Terminal.

Se você tiver mexido em algum arquivo do app, a atualização guarda suas mudanças
antes e devolve depois. Se algo der errado, nada é alterado e seus projetos
continuam intactos.

---

## Se algo quebrar

**O app não abre.** Rode `./install.sh` de novo — ele conserta o que faltar sem
refazer o que já está pronto.

**O navegador reclama do certificado.** Rode `mkcert -install` e recarregue.

**As edições da IA não rodam.** Confira se `claude` (ou `codex`) está instalado e
logado; depois `claude setup-token`.

**Ver o que aconteceu.** O registro do servidor fica em `server.log`, dentro da
pasta do app.

---

## O que fica onde

| pasta | o que é |
|---|---|
| `projects/` | seus vídeos e edições — **só na sua máquina** |
| `~/loom-tools/models/` | modelos de transcrição (compartilhados, ~3,4 GB) |
| `server.log` | registro do servidor |
| `~/Library/Logs/AIVideoStudio-update.log` | registro da última atualização |

Para desinstalar: `launchctl unload ~/Library/LaunchAgents/com.aivideostudio.server.plist`
e apague a pasta do app.

---

## Permissão de uso

Este app é do Renato Klimeck. Você pode usá-lo e modificá-lo à vontade para
fazer os seus próprios vídeos. Não é software de código aberto com licença
formal: não redistribua nem venda como se fosse seu.

## De onde vêm as partes que não são minhas

| o quê | origem | licença |
|---|---|---|
| detector de rosto `face_detection_yunet_2023mar.onnx` | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) | MIT |
| transcrição | [whisper.cpp](https://github.com/ggerganov/whisper.cpp) + modelos ggml | MIT |
| detecção de voz | [Silero VAD](https://github.com/snakers4/silero-vad) | MIT |
| fontes em `web/public/fonts/` | Google Fonts | SIL Open Font License 1.1 |
| vídeo e áudio | [FFmpeg](https://ffmpeg.org) | LGPL/GPL |
