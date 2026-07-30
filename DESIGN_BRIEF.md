# AI Video Studio — Especificação completa (briefing pra design)

## 1. O que é

O **AI Video Studio** é um editor de vídeo local (roda no navegador, no Mac do usuário) com um propósito único: ser o **console de ajuste fino entre um editor de IA (Claude) e um criador de conteúdo humano**. O Claude produz a primeira versão de um edit de Reel/vídeo vertical (cortes, legendas sincronizadas, animações, overlays) e o usuário finaliza no app: acerta legenda, move elemento, estica corte, troca música, exporta. É um "CapCut pessoal", simplificado, focado nas funções que o criador realmente usa.

**O conceito central:** cada vídeo é um arquivo de projeto (`project.json`) compartilhado entre a IA e o humano. O Claude edita esse arquivo; o app edita o mesmo arquivo; o botão Exportar renderiza a partir dele. Isso garante que o preview do app e o vídeo final são a mesma coisa, e que os dois lados continuam o trabalho de onde o outro parou, nos dois sentidos.

**Fluxo típico:**
1. Usuário grava o vídeo bruto e diz ao Claude o que quer
2. Claude entrega a v1 como projeto do app (cortes prontos, legendas palavra a palavra, headlines, overlays)
3. Usuário abre no app, faz ajustes finos (ou pede revisão ao Claude, que atualiza o mesmo projeto)
4. Usuário exporta em qualidade final e posta

**Formato-alvo:** vídeo vertical 9:16 (Reels/TikTok), tipicamente 1080×1920 ou 2160×3840.

## 2. Estrutura de telas

### Tela 1 — Seletor de projetos
- Lista de projetos criados pelo Claude (nome + dimensões, ex. "Kelly Toy · 1080×1920")
- Clique abre o editor
- Estado vazio: "Nenhum projeto ainda"

### Tela 2 — Editor (tela principal, onde tudo acontece)
Layout atual em 4 regiões:
- **Header/toolbar** (topo): navegação, ações de adicionar, undo/histórico, salvar, exportar
- **Player** (centro): preview vertical 9:16 com todos os elementos renderizados
- **Inspector** (painel direito): propriedades do elemento selecionado (muda por contexto)
- **Timeline** (baixo, largura total): clipes + lanes de elementos + playhead

## 3. Funções completas, por área

### 3.1 Header / Toolbar
- **Voltar** ao seletor (com aviso se houver mudanças não salvas)
- **Nome do projeto** + indicador de estado: "● não salvo" (âmbar) / "✓ salvo"
- **+ Legenda** — adiciona legenda no tempo atual do playhead
- **+ Headline** — adiciona bloco de texto/título no tempo atual
- **+ Imagem/Vídeo** — upload de mídia que vira overlay por cima do vídeo
- **+ Música/Áudio** — upload de faixa de áudio (música, SFX)
- **Undo / Redo** (botões + Cmd+Z / Shift+Cmd+Z)
- **Histórico** — abre modal com todas as versões salvas; restaurar qualquer uma (inclusive "desfazer o que o Claude fez")
- **Salvar** (botão + Cmd+S; todo save gera snapshot automático no histórico)
- **Exportar** (botão primário) — abre modal de export

### 3.2 Player / Preview
- Preview vertical 9:16 renderizando **em tempo real, fiel ao export**:
  - vídeo principal já com os cortes aplicados (pula de take em take)
  - legendas karaokê: a palavra sendo falada acende em branco sólido, as demais ficam branco translúcido, sincronizado por palavra
  - textos/headlines com sombra suave e suporte a emoji
  - overlays de imagem (inclusive com fundo removido/PNG recortado)
  - overlays de vídeo (caixa com crop cover, alinhamento topo/centro)
  - zoom animado (punch-in) do clipe atual
  - fade in/out pra preto do clipe atual
  - clipes com fundo removido mostram o preview processado (pessoa + fundo novo)
- **Manipulação direta:** arrastar legendas, textos e overlays direto no preview pra reposicionar (com contorno azul de seleção)
- Clicar no vazio desseleciona
- **Transporte:** play/pause (botão + barra de espaço), avançar/voltar 1 frame, tempo atual / duração total
- Áudio em sincronia: voz do vídeo principal (com fades por clipe), trilhas adicionadas (com volume/fades), áudio de overlay de vídeo (opcional)

### 3.3 Timeline
- **Régua de tempo** com marcações por segundo; clicar/arrastar posiciona o playhead
- **Zoom da timeline** (slider, 12–140 px/segundo)
- **✂️ Dividir** — corta o clipe sob o playhead em dois (base pra remover fundo de só um pedaço, estilo CapCut)
- Info: contagem de clipes + duração total
- **Lane de clipes:** blocos com duração; **alças de trim** nas bordas esquerda/direita (arrastar pra esticar/encurtar contra o vídeo-fonte); selecionado ganha contorno; clipes com remoção de fundo ganham badge "BG" e cor diferente; deletar clipe
- **Lanes de elementos** (uma por tipo, blocos clicáveis posicionados no tempo):
  - Legendas (mostra o texto)
  - Textos/Headlines
  - Overlays (imagem/vídeo)
  - Áudio
- **Playhead** vermelho atravessando tudo

### 3.4 Inspector — seleção: Clipe
- Início / Fim (segundos na fonte, numérico fino)
- Fade in / Fade out (s)
- **Zoom por keyframes:** lista de keyframes (tempo relativo, escala, centro x/y), adicionar/remover; preset **"+ Punch-in 1.15x"** de 1 clique
- **Fundo (remoção tipo CapCut):**
  - checkbox "Remover fundo deste clipe"
  - cor de fundo (color picker) ou **imagem de fundo** (upload)
  - suavização de borda (feather)
  - **"Processar preview do fundo"** — job com % de progresso; preview passa a mostrar o resultado
  - aviso de desatualizado se o clipe mudou depois do processamento ("Clipe mudou — reprocessa")
  - nota: o export final processa em resolução cheia automaticamente
- Deletar clipe

### 3.5 Inspector — seleção: Legenda
- **Edição palavra a palavra:** cada palavra em campo próprio (corrigir erro de transcrição), com o timing dela visível; deletar palavra
- Fonte (Helvetica Bold, Helvetica, Arial Rounded, Arial Black, Arial Bold)
- Cor (picker), tamanho (% da altura do vídeo), posição x/y
- **Dim** (opacidade das palavras não faladas, 0–1)
- Tipo: **karaokê** (highlight sincronizado) ou **estático**
- Deletar legenda

### 3.6 Inspector — seleção: Texto/Headline
- Textarea multilinha (quebras de linha manuais, emoji ok)
- Fonte, cor, tamanho (% altura), posição x/y
- Início / Fim (s), Fade in / Fade out
- Sombra on/off
- Deletar

### 3.7 Inspector — seleção: Overlay (imagem ou vídeo)
- Posição x/y, largura, altura (opcional; vazio = proporção natural)
- Início / Fim, Fade in / Fade out
- Vídeo: **volume** próprio (0 = mudo) e **alinhamento do crop** (centro/topo)
- Imagem: **"Remover fundo da imagem"** (recorte automático, 1 clique)
- Deletar

### 3.8 Inspector — seleção: Áudio
- Início / Fim, **Volume** (0–2), Fade in / Fade out
- Deletar

### 3.9 Modal de Export
- Duas opções: **Qualidade final** (resolução nativa do projeto) e **Teste rápido (720p)**
- Barra de progresso com % durante o render
- Ao concluir: nome do arquivo, **"Mostrar no Finder"**, **"Abrir vídeo"**, Fechar
- Estado de erro com log

### 3.10 Modal de Histórico
- Lista de snapshots por data/hora (gerados a cada save)
- Clicar restaura aquela versão (a atual é preservada como snapshot antes)

### 3.11 Atalhos de teclado
- `Espaço` play/pause · `Cmd+Z` undo · `Shift+Cmd+Z` redo · `Cmd+S` salvar · `Delete` apaga o elemento selecionado

## 4. Comportamentos de sistema (o design precisa acomodar)
- **Estados de job em background** com progresso: exportando, processando fundo, gerando proxy de vídeo enviado
- **Indicador sujo/salvo** sempre visível; aviso ao sair sem salvar
- **Preview desatualizado** (fundo processado ficou velho) — precisa de aviso visível mas não intrusivo
- Preview roda com **proxies 540p** (leveza), export usa a fonte em resolução cheia
- App desktop no navegador, tela cheia, tema **escuro** (padrão de editor de vídeo), interface em **português (pt-BR)**

## 5. O que se espera do design
- Identidade visual do **AI Video Studio** (nome do app; hoje o placeholder é 🎬)
- Design completo das 2 telas + 2 modais + todos os estados (vazio, selecionado por tipo, jobs com progresso, stale, erro, salvo/não salvo)
- Hierarquia clara: o player é o herói; timeline confortável de manipular (alças de trim, blocos, playhead); inspector denso porém escaneável
- Referências de categoria: CapCut (familiaridade pro usuário), Descript, DaVinci — mas mais simples que todos
- **Restrição de implementação:** será implementado no app existente (React + CSS puro, sem lib de componentes); design tokens (cores, espaçamentos, raios, tipografia) facilitam a implementação fiel
