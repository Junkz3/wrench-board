<p align="center">
  <img src="docs/assets/wrench-mascot.svg" alt="Wrench Board mascot" width="160" />
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <strong>Français</strong> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a>
</p>

# Wrench Board

> Atelier de diagnostic agent-native pour la réparation électronique au niveau
> carte, propulsé par Claude Opus 4.8. **Le droit à la réparation, construit au
> grand jour, par les gens qui font vraiment les réparations.**

🥈 **2e place** au hackathon *Build with Opus 4.7* d'Anthropic, avril 2026.

**📺 Vidéo de démo (3 min) :** https://youtu.be/OZ2D_p82z6w

![Wrench Board : boardview + agent de diagnostic sur une carte mère MNT Reform](docs/assets/screenshot-workbench.png)

## Ce que c'est

Des dizaines de millions de tonnes d'électronique finissent en déchets chaque
année. Une grande partie est récupérable au niveau de la carte : un
condensateur mort, une diode grillée, un PMIC défectueux, mais seul un
technicien en microsoudure peut le trouver et le réparer. Nous sommes le
**dernier kilomètre** de la réparation avant la décharge, et nous ne sommes
pas nombreux.

Wrench Board est un coéquipier senior en microsoudure conçu pour ce dernier
kilomètre. Pour le technicien chevronné, c'est une deuxième paire d'yeux qui
ne se fatigue jamais. Pour l'apprenti, c'est un coéquipier senior qui explique
la séquence de boot pour la dixième fois, dans sa langue, avec ses outils, sans
jugement. Il ingère un schéma PDF et un boardview, construit un pack de
connaissances par appareil en deux minutes, et fait tourner un agent de
diagnostic Opus 4.8 qui pilote la carte visuellement (il met en surbrillance
les pins, suit les nets, simule les pannes) pendant que le technicien garde le
fer à la main.

Il est agnostique vis-à-vis de l'appareil. Donnez-lui un schéma et un boardview
et il fonctionne pareil sur les cartes mères iPhone et MacBook, les téléphones
Android et Samsung, les cartes mères de consoles de jeu, les ordinateurs
portables et les ordinateurs monocartes. Tout ce qui a un schéma et un boardview
est dans le périmètre.

Le pari, c'est **la précision plutôt que la magie**. L'agent n'a pas le droit
d'inventer un reference designator. Chaque refdes qu'il prononce provient d'une
recherche par un outil, et un sanitizer côté serveur encadre tout token qu'il
ne peut pas vérifier *avant* que le texte n'atteigne l'écran. Les moteurs
déterministes en dessous produisent des chaînes causales vérifiables, pas des
impressions.

## Pourquoi ça existe

Je suis technicien en microsoudure depuis trois ans. Pendant la majeure partie
de ce temps, j'envoyais des captures d'écran à Claude une par une, à la main,
et je collais la réponse dans un cahier papier. J'ai construit l'atelier dont
j'avais besoin.

## Comment ça marche

Quatre workflows orthogonaux alimentent un seul corpus sur disque par appareil
sous `memory/{slug}/` :

- **Knowledge Factory** : quatre personas Claude (Scout, Registry, Writers,
  Auditor) construisent un pack de réparation vérifié à partir d'une étiquette
  d'appareil en environ 2 minutes. Les trois Writers (Cartographe / Clinicien /
  Lexicographe) tournent en parallèle et partagent un préfixe chauffé en cache
  pour amortir la longue entrée partagée entre les writers.
- **Schematic Ingestion** : la vision d'Opus 4.8 compile un schéma PDF, page
  par page, en un `ElectricalGraph` interrogeable : nets classifiés, séquence
  de boot inférée, rapport de qualité attaché.
- **Diagnostic Agent** : un Anthropic Managed Agent par appareil, avec une
  mémoire en quatre couches (`global-patterns`, `global-playbooks`,
  `device-{slug}`, `repair-{repair_id}`), pilote le boardview via 17 outils
  `bv_*` et interroge le pack, le graphe schématique, les mesures, les
  validations et le profil du technicien via 27 autres : 44 outils
  custom déclarés dans `api/agent/manifest.py`. L'agent ne fabrique jamais de
  refdes : discipline d'outils plus un sanitizer a posteriori.
- **microsolder-evolve** : quatre boucles de recherche nocturnes, une par
  surface : le simulateur déterministe + les moteurs hypothesize (`sim`), le
  compilateur de schéma (`pipeline`), la passe de vision schématique
  (`pipeline-vision`) et l'agent de diagnostic lui-même (`agent`). Chaque
  boucle propose des patches contre un benchmark oracle et soit les garde
  (commit préfixé `evolve:`) soit les annule. Les boucles tournent et livrent
  des améliorations pendant que je travaille sur autre chose.

![Wrench Board : tableau de bord de réparation avec artefacts de connaissances et fils de diagnostic](docs/assets/screenshot-dashboard.png)

### Fichiers + Vision : l'agent peut demander à voir

Un diagnostic en microsoudure se joue sur ce que la pointe de mesure touche
*à l'instant*, et une zone de chat ne peut pas transporter ça. Le technicien
branche un microscope USB ou une webcam dans l'atelier et l'agent demande une
image à la volée via l'outil `cam_capture`, lit l'image, et la réinjecte dans
son raisonnement. Le technicien peut aussi déposer une macro ou un gros plan
d'une puce suspecte dans le chat à tout moment. Les captures et les uploads
sont persistés sous la réparation pour qu'une session puisse être rejouée de
bout en bout : les mots, les décisions, et les photographies réelles que
l'agent a regardées.

Ça boucle la boucle que le workflow de collage de captures d'écran n'a jamais
pu fermer : l'agent arrête de *deviner* à quoi ressemble la carte et commence
à la *voir*, au signal du technicien, sur l'optique du technicien.

## Sous le capot

- **Backend** : Python 3.11+ / FastAPI / WebSocket natif / Pydantic v2 /
  pdfplumber. Pas d'étape de build, pas de bundler.
- **Frontend** : HTML + CSS + JS vanilla, tokens de design OKLCH, D3 v7 pour
  le graphe de connaissances et Three.js r128 (WebGL) pour le boardview.
  Icônes SVG inline. Pas de framework.
- **Modèles** : Claude Opus 4.8 (writers lourds du pipeline, vision
  schématique, niveau de diagnostic `deep`), Claude Sonnet 4.6 (Scout,
  Registry, Mapper, Lexicographe, niveau `normal`), Claude Haiku 4.5
  (classifieur d'intention, narrateur de phase, gate de couverture, niveau
  `fast`).
- **Mémoire** : magasins de mémoire Anthropic Managed Agents par appareil.
  L'agent se réoriente entre les sessions en lisant son propre cahier de
  scribe (`state.md`, `decisions/`, `measurements/`, `open_questions.md`) au
  lieu de se fier à un résumé généré par LLM.
- **Boardview** : 16 parsers dans `api/board/parser/`, dispatchés par
  extension : KiCad `.kicad_pcb`, OpenBoardView Test_Link `.brd`,
  KiCad-boardview BRD2, plus `.asc` `.bdv` `.bv` `.bvr` `.cad` `.cst` `.f2b`
  `.fz` `.gr` `.pcb` `.tvw`. Ajouter un format = un seul nouveau fichier.
- **Tests** : 2 600+ tests rapides (~1 min) plus une suite `@slow` de gate de
  précision, incluant 10 invariants déterministes sur le simulateur + les
  moteurs hypothesize et des gates oracle figés.
- **Outillage** : `make doctor` lance 8 checks de santé locaux (env, packs,
  parsers, caméra) pour le déploiement en atelier. `make eval-all` orchestre
  les quatre surfaces d'éval (simulateur, pipeline, vision, agent) avec
  détection de régression cross-skill. `make tools-inventory` écrit un index
  local de manifeste d'agent pour revue hors-ligne.
- **Anti-hallucination** : défense en profondeur, deux couches. (1) Les outils
  renvoient `{found: false, closest_matches: [...]}` pour un refdes inconnu ;
  le prompt système enjoint l'agent de choisir parmi les suggestions ou de
  demander à l'utilisateur. (2) `api/agent/sanitize.py` scanne chaque texte
  sortant à la recherche de tokens en forme de refdes
  (`\b[A-Z]{1,3}\d{1,4}\b`) et encadre tout match non vérifié en `⟨?U999⟩`
  avant qu'il n'atteigne le technicien.

Deux moteurs déterministes purement synchrones (`simulator.py`,
`hypothesize.py`) sont au cœur de la pile de diagnostic. Le simulateur avance
phase par phase sur une séquence de boot et émet une timeline des rails morts,
des composants morts, et de la cause du blocage par phase. L'hypothèseur prend
une observation partielle et énumère les candidats refdes-kill à 1 et 2 fautes
qui l'expliquent, classés par F1 contre l'observation. Aucun des deux n'appelle
de LLM au runtime.

L'agent de diagnostic a deux runtimes interchangeables : **managed** via
Anthropic Managed Agents, **direct** via la Messages API. Managed est le mode
par défaut et le chemin de production ; direct sert de fallback quand la beta MA
est indisponible et de harnais d'inspection sur disque pendant le
développement. Le protocole WebSocket est identique, donc le frontend ne sait
pas lequel tourne.

## Roadmap : Community Evolution Loop

Wrench Board tourne en local. L'instance de chaque technicien peut améliorer
son simulateur déterministe contre ses propres cas de terrain. Quand la boucle
evolve découvre une règle qui tient la route, elle fait remonter une pull
request candidate vers le dépôt amont. Le droit à la réparation, construit au
grand jour, par les gens qui font vraiment les réparations.

## Quickstart

```bash
git clone https://github.com/Junkz3/wrench-board
cd wrench-board
make install          # create .venv and install deps (incl. [dev])
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
make run              # uvicorn --reload on http://localhost:8000
```

Au premier `make run` en mode Managed Agents (par défaut), le script de
démarrage affiche un avertissement d'un écran décrivant ce qu'il s'apprête à
créer sur votre compte Anthropic (1 environnement + 3 agents par niveau,
inactifs, sans coût jusqu'à utilisation) et attend 5 secondes un Ctrl+C avant
le bootstrap. Les IDs atterrissent dans `managed_ids.json` (gitignored) et les
exécutions suivantes vont directement à uvicorn.

Repli vers le mode direct si la beta Managed Agents est indisponible sur votre
compte : pas de bootstrap, simple boucle d'outils `messages.create` :

```bash
make demo-fallback
# or: DIAGNOSTIC_MODE=direct make run
```

## Licence et crédits

Source-available sous une licence propriétaire, voir [`LICENSE`](LICENSE).
Gratuit pour l'évaluation personnelle, l'étude et l'usage local. **Les
professionnels indépendants de la réparation électronique peuvent aussi
l'utiliser comme outil interne lorsqu'ils interviennent pour leurs propres
clients** (rémunération commerciale OK), sans licence séparée nécessaire. La
redistribution, le déploiement SaaS hébergé, le sous-licenciement, et tout
usage pour l'entraînement de modèles d'IA / ML concurrents requièrent toujours
une autorisation écrite (contact : alexis@repairmind.co.uk). Les dépendances
sont uniquement MIT / Apache 2.0 / BSD. La carte mère MNT Reform utilisée comme
cible de test canonique est sous CERN-OHL-S-2.0. Construit en solo à Repair
Valley, un atelier indépendant de réparation électronique.

## Contribuer

Wrench Board est ouvert aux contributeurs qui tiennent au droit à la
réparation. Rapports de terrain, nouveaux parsers de boardview, règles de
simulateur : ouvrez une issue ou une PR.
