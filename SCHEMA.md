# project.json — schema v2

O `project.json` é o contrato entre o Claude, o app (preview) e o renderer (export).
O que o preview mostra tem que sair idêntico no export. Campos internos (marcados
_interno_) existem pro app/renderer funcionarem, mas não aparecem na UI e o design
não depende deles. Chaves de raiz prefixadas com `_` são descartadas pelo server no save.

## Raiz

```jsonc
{
  "version": 2,
  "name": "Kelly Toy",
  "w": 1080, "h": 1920,          // dimensões reais do projeto em px
  "aspect": "9:16",              // "9:16" | "16:9" — guia player + export
  "fps": 30,                     // interno · fps do render (default 30)
  "by": "claude",                // "you" | "claude" — último autor. O app salva com
                                 // by:"you" (via _by no PUT); o Claude, ao editar o
                                 // arquivo diretamente, seta by:"claude".
                                 // editedAt = mtime do arquivo (não é campo).
  "sources": {                   // interno · fontes de vídeo ("takes")
    "main":  { "path": "/abs/ou/rel.mp4", "proxy": "media/proxy_main.mp4", "duration": 249.4,
               "strip": "media/strip_ab12.jpg", "stripInterval": 1, "stripFrames": 250,  // interno · filmstrip
               "transcript": "media/transcript_main.json" },  // interno · palavras em tempo de FONTE (REN-83)
    "s1699…": { "path": "media/169…_take.mp4", "proxy": "media/proxy_169….mp4", "duration": 31.2 }
  },
  "tracks": {                    // controles por track (REN-112; defaults false)
    "video":    { "muted": false, "hidden": false, "locked": false },
    "captions": { "hidden": false, "locked": false },
    "texts":    { "hidden": false, "locked": false },
    "overlays": { "muted": false, "hidden": false, "locked": false },
    "audio":    { "muted": false, "locked": false }
  },  // muted → fora do mix do export; hidden → não desenha (preview E export);
      // locked → só editor (não seleciona/arrasta/trima/deleta). Objeto/chaves
      // ausentes = tudo false. Alturas de track NÃO ficam aqui (localStorage).
  "clips": [ … ], "captions": [ … ], "texts": [ … ], "overlays": [ … ], "audios": [ … ]
}
```

**Modelo de tempo:** os clipes são concatenados. Início do clipe *i* na timeline =
Σ `(out − in)` dos anteriores. Duração total = Σ `(out − in)`. Tempos de
captions/texts/overlays/audios são segundos de **TIMELINE** (não da fonte).

## clips[]

```jsonc
{
  "id": "c1",
  "src": "main",                 // interno · chave em sources{}
  "in": 0.0, "out": 5.2,         // segundos na FONTE
  "fadeIn": 0.2, "fadeOut": 0,   // fade de/para preto (s)
  "vol": 1,                      // volume do áudio do clipe 0–2 (default 1; REN-114)
  "note": "take 2 of 4",         // rótulo curto em INGLÊS (opcional; ausente/vazio =
                                 // sem rótulo). Só a UI lê: aparece na etiqueta do
                                 // clipe na timeline no lugar do "take N" posicional
                                 // (dois números de take no mesmo clipe se contradizem).
                                 // Renderer e export ignoram. Convenção "take N of M":
                                 // clipes CONSECUTIVOS formando a série 1..M completa
                                 // ganham um trilho de grupo no topo — é como as
                                 // tentativas repetidas da mesma fala se leem como um
                                 // conjunto. Qualquer outro texto vira rótulo simples,
                                 // sem trilho.
  "fadeIn": 0, "fadeOut": 0,     // fade da IMAGEM para preto (s)
  "aFadeIn": 0, "aFadeOut": 0,   // fade do ÁUDIO (s) — REN-125. Separado do de
                                 // vídeo: a bolinha na waveform mexe só no som,
                                 // como no CapCut. Ausente = usa o de vídeo
                                 // (projetos anteriores à separação; ver
                                 // scripts/migrate_afade.py). Vale para clips
                                 // e overlays de vídeo. Em audios[] o
                                 // fadeIn/fadeOut já era só de áudio.
  "kfs": [                       // keyframes de zoom; t relativo ao início do clipe
    { "t": 0, "scale": 1, "cx": 50, "cy": 50 },   // cx/cy em % 0–100 (50 = centro)
    { "t": 5.2, "scale": 1.15, "cx": 50, "cy": 45 },
    { "t": 0, "scale": 1.07, "cx": 48, "cy": 42, "auto": true }
                                 // auto:true = posto pelo botão Auto zoom
                                 // (REN-159). Um segundo clique remove SÓ estes;
                                 // keyframe sem a marca é dele e nunca é tocado.
                                 // O renderer ignora o campo.
  ],
  "bg": {                        // remoção de fundo (null = off)
    "color": "#0E3B34", "feather": 4,
    "image": null,               // caminho relativo de imagem de fundo (opcional)
    "processed": true,           // preview já processado
    "stale": false,              // clipe editado depois do processamento
    "_cache": { "path": "cache/segbg_c1.mp4", "in": 0.0, "out": 5.2 }  // interno
  },
  "rt": {                        // face retouch (null = off)
    "preset": "natural",         // "natural" | "studio" | "custom"
    "intensity": 100,            // 0–100, mestre — TRIM puro (m = intensity/100).
                                 // Abre em 100 para o slider valer o que diz;
                                 // fine-tunes começam em 0 (REN-177)
    "smooth": 30, "even": 25, "blem": 20, "dewrinkle": 20, "shine": 15,
    "plump": 10, "eyes": 20, "circles": 10,   // fine-tune 0–100
                                 // dewrinkle (REN-160): levanta LINHAS escuras na
                                 // banda entre poro e sombra; poro fica intacto
    "scope": "all",              // "all" (default) | "clip"
                                 // "clip" = fixado: uma mudança global NÃO o
                                 // sobrescreve. Só apertar "All clips" o toma.
    "processed": true,           // preview processado (real pipeline) disponível
    "stale": false,              // clipe/sliders mudaram depois do processamento
    "_cache": { "path": "cache/retouch_c1.mp4", "in": 0.0, "out": 5.2,
                "sig": "50,30,25,20,20,15,10,20,10" }  // interno · in/out/sig p/ validar frescor
                                 // sig segue RT_KEYS em store.js — um slider fora
                                 // dessa lista deixa o preview processado velho
  }
}
```

## captions[] (grupos karaokê)

```jsonc
{
  "id": "g1",
  "x": 50, "y": 76,              // % do frame, âncora central
  "size": 3.4,                   // % da ALTURA do vídeo
  "font": "Helvetica Bold",      // Helvetica Bold | Helvetica | Arial Rounded | Arial Black | Arial Bold
  "color": "#FFFFFF",
  "dim": 0.45,                   // opacidade das palavras não faladas (0–1)
  "mode": "karaoke",             // "karaoke" | "static"
  "maxW": null,                  // largura da caixa em % da LARGURA do frame
                                 // (20–96; null = 86). Controla palavras por linha.
  "capAnchor": "source",         // REN-115 · legenda ancorada na FONTE
  "src": "main",                 // chave em sources{} a que o grupo pertence
  "words": [{ "w": "this", "t0": 7.13, "t1": 7.25 }]  // t0/t1 = segundos da FONTE
}
```

**Source-anchored (REN-115):** com `capAnchor:"source"`, as palavras têm `t0/t1`
em segundos da **FONTE** (`src`), como `clips.in/out`. O tempo de TIMELINE é
DERIVADO no desenho via `sourceToTimeline` (primeiro clip que cobre o momento):
a palavra só aparece se o momento da fonte estiver coberto por um clip do `src`
— cortar o vídeo faz a palavra sumir e o resto refluir, sem estado paralelo.
Não há `resyncCaptions`; a legenda é invariante a trim/split. Grupo sem
`capAnchor` = legado (palavras `{w,t,d}` em TIMELINE). `render/common.py`
(`materialize_cap`) e `web/src/time.js` (`materializeCap`) dobram o grupo pro
formato de timeline com o MESMO código — preview = export.

Render (preview E export): bold 700, font-size = `size% × altura renderizada`,
line-height 1.25, sombra `0 2px 10px rgba(0,0,0,0.7)` (escala com a altura, ref. 640px),
gap entre palavras `0.32em`, quebra centrada, largura máx 86% do frame. Palavra falada
(`t ∈ [w.t, w.t+w.d)`) na cor da caption; as demais `rgba(255,255,255, dim)`.
O grupo fica visível de `words[0].t` até `última.t + última.d + 0.15`.

## texts[] (headlines)

```jsonc
{
  "id": "t1", "text": "THE MOST\nVIRAL TOY 🔥",   // multilinha + emoji
  "x": 50, "y": 14, "size": 4.6, "color": "#FFFFFF",
  "t0": 0, "t1": 5.2, "fadeIn": 0.2, "fadeOut": 0.2, "shadow": true,
  "maxW": null,                  // largura de quebra em % da largura (20–96; null = 90)
  "font": "Arial Rounded"        // interno · fonte de render (UI não expõe)
}
```

## overlays[]

```jsonc
{
  "id": "o1",
  "kind": "image",               // "image" | "video"
  "name": "unboxing.png",        // exibição
  "path": "media/unboxing.png",  // interno
  "proxy": null,                 // interno · proxy 540p (vídeo)
  "cutPath": null,               // interno · versão com fundo removido (imagem)
  "x": 50, "y": 38, "w": 58,     // % (h opcional; null = auto pela proporção da mídia)
  "h": null,
  "t0": 6.0, "t1": 11.5, "fadeIn": 0.25, "fadeOut": 0.25,
  "cut": true,                   // imagem: usar versão sem fundo
  "vol": 0,                      // vídeo: volume 0–1
  "crop": "center"               // vídeo: "center" | "top"
}
```

Caixa posicionada pelo centro em x/y, largura `w%`; cantos arredondados (radius 10
proporcional, ref. 640px) no preview e no export.

## audios[]

```jsonc
{
  "id": "a1", "name": "lofi-track.mp3",
  "path": "media/lofi.mp3",      // interno
  "t0": 0, "t1": 20,             // t1 null = até o fim
  "vol": 0.35,                   // 0–2
  "fadeIn": 1.0, "fadeOut": 1.5
}
```

## Diferenças vs v1 (migração: `scripts/migrate_v2.py`)

| v1 | v2 |
|---|---|
| `width`/`height` | `w`/`h` + `aspect` |
| `timeline[]` | `clips[]` (+`src`) |
| `zoom[] {t,scale,cx,cy}` frações | `kfs[]` com cx/cy em % 0–100 |
| `bg {enabled, previewCache, cachedIn/Out}` | `bg` null quando off; `processed`/`stale` + `_cache` |
| — | `rt` (face retouch) |
| captions com `words[{w,t0,t1}]` anchor source | `words[{w,t,d}]` em tempo de TIMELINE |
| `dimAlpha` / `type` karaoke\|plain | `dim` / `mode` karaoke\|static |
| x/y/size/w/h frações 0–1 | % 0–100 (size = % da altura) |
| `audio[] {gain}` | `audios[] {vol}` |
| overlay `type/volume/align`, cutout troca o path | `kind/vol/crop` + `cut` reversível (`cutPath`) |
