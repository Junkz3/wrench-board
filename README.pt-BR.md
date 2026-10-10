<p align="center">
  <img src="docs/assets/wrench-mascot.svg" alt="Mascote do Wrench Board" width="160" />
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <strong>Português</strong>
</p>

# Wrench Board

<p align="center">
  <a href="https://github.com/Junkz3/wrench-board/stargazers"><img src="https://img.shields.io/github/stars/Junkz3/wrench-board?style=flat&color=63d1de" alt="Estrelas no GitHub"></a>
  <img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-source--available-lightgrey" alt="Licença: source-available">
  <img src="https://img.shields.io/badge/powered%20by-Claude%20Opus%204.8-d97757" alt="Powered by Claude Opus 4.8">
</p>

> Bancada de diagnóstico nativa para agentes, voltada ao reparo de eletrônicos em nível de placa,
> com a força do Claude Opus 4.8. **Direito ao reparo, desenvolvido abertamente por quem
> realmente faz os reparos.**

🥈 **2º lugar** no hackathon *Build with Opus 4.7* da Anthropic, em abril de 2026.

**📺 Vídeo de demonstração (3 min):** https://youtu.be/OZ2D_p82z6w

**Experimente online:** [abra um boardview no navegador, grátis](https://wrenchboard.cloud/boardview-viewer). Sem conta, sem instalação: `.brd`, `.fz`, `.tvw`, `.pcb` e outros, interpretados nativamente.

![Wrench Board: boardview + agente de diagnóstico em uma placa-mãe MNT Reform](docs/assets/screenshot-workbench.png)

## O que é

Dezenas de milhões de toneladas de eletrônicos viram lixo eletrônico todos os anos. Uma grande parte disso pode ser recuperada no nível da placa: um capacitor morto, um diodo queimado, um PMIC defeituoso, mas apenas um técnico de microssolda consegue encontrar e consertar. Nós somos a **última milha** do reparo antes do aterro, e não somos muitos.

Wrench Board é um colega sênior de microssolda criado para essa última milha. Para o técnico experiente, é um segundo par de olhos que nunca se cansa. Para o aprendiz, é um colega sênior que explica a sequência de boot pela décima vez, na sua linguagem, com suas ferramentas, sem julgamentos. Ele ingere um PDF de esquemático e um boardview, monta um pacote de conhecimento por dispositivo em dois minutos e executa um agente de diagnóstico Opus 4.8 que pilota a placa visualmente (destaca pinos, traça linhas, simula falhas) enquanto o técnico mantém o ferro de solda na mão.

É agnóstico a dispositivos. Forneça um esquemático e um boardview e ele funciona da mesma forma em placas lógicas de iPhone e MacBook, celulares Android e Samsung, placas-mãe de consoles de videogame, notebooks e computadores de placa única. Qualquer coisa com um esquemático e um boardview é válida.

A aposta é **precisão acima de mágica**. O agente não tem permissão para inventar um reference designator. Cada refdes que ele menciona vem de uma consulta a ferramentas, e um sanitizador no servidor encapsula qualquer token que não possa verificar *antes* de o texto chegar à tela. Os mecanismos determinísticos subjacentes produzem cadeias causais verificáveis, não suposições.

## Por que existe

Sou técnico de microssolda há três anos. Durante a maior parte desse tempo, enviei capturas de tela para o Claude uma por uma, manualmente, e colei as respostas em um caderno de papel. Criei a bancada de trabalho que eu precisava.

## Como funciona

Quatro fluxos de trabalho ortogonais alimentam um único corpus em disco por dispositivo em
`memory/{slug}/`:

- **Fábrica de Conhecimento**: quatro personas do Claude (Scout, Registry, Writers,
  Auditor) criam um pacote de reparo verificado a partir da etiqueta do dispositivo em ~2 minutos.
  Os três Writers (Cartographe / Clinicien / Lexicographe) rodam em
  paralelo e compartilham um prefixo com cache aquecido para amortizar a longa entrada
  compartilhada entre os writers.
- **Ingestão de Esquemático**: o Opus 4.8 vision compila um esquemático em PDF, página
  por página, em um `ElectricalGraph` consultável: nets classificadas, sequência
  de boot inferida e relatório de qualidade anexado.
- **Agente de Diagnóstico**: um Anthropic Managed Agent por dispositivo, com uma
  memória em camadas de quatro níveis (`global-patterns`, `global-playbooks`,
  `device-{slug}`, `repair-{repair_id}`), pilota o boardview através de 17
  ferramentas `bv_*` e consulta o pacote, grafo do esquemático, medições,
  validações e perfil do técnico através de mais 27: 44 ferramentas personalizadas
  declaradas em `api/agent/manifest.py`. O agente nunca inventa um
  refdes: disciplina das ferramentas mais um sanitizador posterior.
- **microsolder-evolve**: quatro loops de busca noturnos, um
  por superfície: o simulador determinístico + motores de hipótese
  (`sim`), o compilador de esquemático (`pipeline`), a passagem de visão
  do esquemático (`pipeline-vision`) e o próprio agente de diagnóstico (`agent`).
  Cada loop propõe patches contra um benchmark oráculo e os mantém
  (commit com prefixo `evolve:`) ou os reverte. Os loops têm
  rodado e entregando melhorias enquanto eu trabalho em outras coisas.

![Wrench Board: painel de reparo com artefatos de conhecimento e threads de diagnóstico](docs/assets/screenshot-dashboard.png)

### Arquivos + Visão: o agente pode pedir para ver

Um diagnóstico de microssolda depende totalmente do que a ponta de prova está tocando
*agora*, e uma caixa de chat não consegue transmitir isso. O técnico conecta um microscópio USB
ou webcam à bancada e o agente solicita um frame sob
demanda através da ferramenta `cam_capture`, lê a imagem e a incorpora
ao seu raciocínio. O técnico também pode soltar uma foto macro ou um close-up
de um chip suspeito no chat a qualquer momento. Capturas e uploads são
persistidos no reparo para que uma sessão possa ser reproduzida de ponta a ponta:
texto, decisões e as fotografias reais que o agente analisou.

Isso fecha o ciclo que o fluxo de colar screenshots nunca conseguiu: o
agente para de *adivinhar* como a placa se parece e passa a *vê-la*,
sob comando do técnico, através da ótica do técnico.

## Por baixo do capô

- **Backend**: Python 3.11+ / FastAPI / WebSocket nativo / Pydantic v2 /
  pdfplumber. Sem etapa de build, sem bundler.
- **Frontend**: HTML + CSS + JS puro, tokens de design OKLCH, D3 v7 para o
  grafo de conhecimento e Three.js r128 (WebGL) para o boardview. Ícones
  SVG inline. Sem framework.
- **Modelos**: Claude Opus 4.8 (escritores de pipelines pesados, visão de
  esquemático, nível `deep`), Claude Sonnet 4.6 (Scout, Registry, Mapper,
  Lexicographe, nível `normal`), Claude Haiku 4.5 (classificador de intenção,
  narrador de fase, porta de cobertura, nível `fast`).
- **Memória**: armazenamentos de memória Anthropic Managed Agents por dispositivo. O
  agente se reorienta entre sessões lendo seu próprio caderno de anotações
  (`state.md`, `decisions/`, `measurements/`, `open_questions.md`)
  em vez de depender de um resumo gerado por LLM.
- **Boardview**: 16 parsers em `api/board/parser/`, despachados
  por extensão: KiCad `.kicad_pcb`, OpenBoardView Test_Link `.brd`,
  KiCad-boardview BRD2, além de `.asc` `.bdv` `.bv` `.bvr` `.cad` `.cst`
  `.f2b` `.fz` `.gr` `.pcb` `.tvw`. Adicionar um formato = um novo arquivo.
- **Testes**: mais de 2.600 testes rápidos (~1 min) além de uma suíte de porta de precisão `@slow`,
  incluindo 10 invariantes determinísticos nos motores simulator e hypothesize
  e portas de oráculo congelado.
- **Ferramentas**: `make doctor` executa 8 verificações de integridade locais (env, pacotes,
  parsers, câmera) para implantação no ateliê. `make eval-all` orquestra
  as quatro superfícies de avaliação (simulador, pipeline, visão, agente) com
  detecção de regressão entre habilidades. `make tools-inventory` gera um
  índice de manifesto de agente local para revisão offline.
- **Anti-alucinação**: defesa em profundidade, duas camadas. (1) Ferramentas retornam
  `{found: false, closest_matches: [...]}` para refdes desconhecidos; o prompt
  do sistema instrui o agente a escolher entre as sugestões ou perguntar ao usuário.
  (2) `api/agent/sanitize.py` varre todo texto de saída em busca de tokens com formato de refdes
  (`\b[A-Z]{1,3}\d{1,4}\b`) e envolve qualquer correspondência não verificada como
  `⟨?U999⟩` antes que chegue ao técnico.

Dois motores determinísticos puramente síncronos (`simulator.py`, `hypothesize.py`) estão
no centro da pilha de diagnóstico. O simulador avança fase a fase
sobre uma sequência de boot e emite uma linha do tempo de linhas mortas, componentes mortos
e a causa do bloqueio por fase. O motor de hipóteses recebe uma observação parcial
e enumera candidatos a refdes causadores de falha simples ou dupla que
a explicam, classificados por F1 em relação à observação. Nenhum deles chama um LLM em
tempo de execução.

O agente de diagnóstico possui dois runtimes intercambiáveis: **gerenciado** via
Anthropic Managed Agents, **direto** via Messages API. O gerenciado é o
padrão e o caminho de produção; o direto serve como fallback quando o beta do MA
está indisponível e como ambiente de inspeção em disco durante o
desenvolvimento. O protocolo WebSocket é idêntico, então o frontend não
sabe qual deles está em execução.

## Roadmap: Ciclo de Evolução da Comunidade

O Wrench Board roda localmente. A instância de cada técnico pode aprimorar seu simulador determinístico com base nos próprios casos de campo. Quando o ciclo de evolução identifica uma regra que se confirma, ele gera um pull request candidato para o repositório upstream. Direito ao reparo, desenvolvido abertamente, pelas pessoas que realmente fazem os reparos.

## Início Rápido

```bash
git clone https://github.com/Junkz3/wrench-board
cd wrench-board
make install          # cria .venv e instala dependências (incl. [dev])
cp .env.example .env  # depois preencha ANTHROPIC_API_KEY
make run              # uvicorn --reload em http://localhost:8000
```

Na primeira execução de `make run` no modo Managed Agents (padrão), o script
de inicialização exibe um aviso de uma tela descrevendo o que será criado
na sua conta Anthropic (1 ambiente + 3 agentes com escopo por tier, ociosos,
sem custo até o uso) e aguarda 5 segundos por Ctrl+C antes do bootstrap.
Os IDs são salvos em `managed_ids.json` (ignorado pelo git) e as execuções
seguintes iniciam direto no uvicorn.

Use o modo direto como fallback se o beta Managed Agents não estiver
disponível na sua conta, sem bootstrap, apenas loop simples da ferramenta
`messages.create`:

```bash
make demo-fallback
# ou: DIAGNOSTIC_MODE=direct make run
```

## Licença e créditos

Código-fonte disponível sob uma licença proprietária. Consulte [`LICENSE`](LICENSE).
Gratuito para avaliação pessoal, estudos e uso local. **Profissionais
independentes de reparo de eletrônicos também podem usá-lo como ferramenta interna
ao atender seus próprios clientes** (remuneração comercial permitida), sem
necessidade de licença separada. Redistribuição, implantação SaaS hospedada,
sublicenciamento e qualquer uso para treinamento de modelos concorrentes de IA/ML ainda
exigem autorização por escrito (contato: alexis@repairmind.co.uk).
As dependências são exclusivamente MIT / Apache 2.0 / BSD. A placa-mãe do MNT Reform
usada como alvo de teste canônico está sob a licença CERN-OHL-S-2.0. Desenvolvido individualmente na
Repair Valley, uma oficina independente de reparo de eletrônicos.

## Contribuindo

O Wrench Board está aberto a contribuidores que se importam com o direito ao reparo.
Relatórios de campo, novos parsers de boardview, regras de simulador: abra uma issue ou
um PR.
