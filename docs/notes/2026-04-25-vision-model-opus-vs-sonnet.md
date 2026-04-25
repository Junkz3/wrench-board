# Vision pipeline — pourquoi Opus 4.7 (et pas Sonnet 4.6)

**Date :** 2026-04-25
**Décision :** Garder `claude-opus-4-7` comme modèle par défaut sur `api/pipeline/schematic/page_vision.extract_page`.
**Statut :** Validé empiriquement, ne pas re-tester sans nouveau modèle vision (4.8+, ou Sonnet 4.7+).

## Question testée

Sonnet 4.6 est ~40 % moins cher qu'Opus 4.7 ($3/$15 vs $5/$25 par MTok). Sur un pipeline qui ingère ~20-100 pages par device, le différentiel est significatif (~$4-10 par device). Est-ce que Sonnet est viable pour la vision schematic, ou perd-il du signal critique ?

## Méthodologie

1. Pris une page représentative et dense du pack iPhone X déjà ingéré : `memory/iphone-x/schematic_pages/page-04.png` (= page 15 du PDF original = SOC: Power 1/3 - CPU, GPU & SOC RAILS).
2. Lancé `page_vision.extract_page` deux fois sur la même page avec :
   - Opus 4.7 (résultat déjà sur disque : `page_004.json`)
   - Sonnet 4.6 (résultat ad-hoc : `/tmp/iphone_sonnet_page04.json`)
3. Comparé refdes-by-refdes et net-by-net.
4. Vérification ground-truth : `pdfplumber.extract_text()` sur la page → vérification que chaque refdes/net émis par le modèle apparaît verbatim dans le texte du PDF.

Script de référence : `/tmp/sonnet_vs_opus_page.py` (à régénérer si re-test).

## Résultats

### Couverture

| Élément | Opus 4.7 | Sonnet 4.6 | Verdict |
|---|---|---|---|
| Capacitors | 40 | 40 | Identique |
| IC (U1000 SoC) | 1 | 1 | Identique |
| Nets (count) | 30 | 30 | Identique |
| Edges typées | 60 | 54 | Sonnet -10 % |
| Cross-page refs | 16 | 19 | Sonnet +19 % |
| Designer notes | 0 | 16 | Sonnet capture les annotations |
| Ambiguities flagged | 0 | 7 | Sonnet plus honnête sur ses incertitudes |

### Hallucinations (vérification ground-truth contre PDF text)

| Modèle | Refdes inventés | Nets inventés |
|---|---|---|
| **Opus 4.7** | **0 / 45** | 1 / 30 (uniquement `GND` — symbole, pas texte ; non-bug) |
| **Sonnet 4.6** | 0 / 105 | **3 / 30** confirmés inventés |

Les 3 nets hallucinés par Sonnet :

| Sonnet a dit | PDF dit en vrai | Type d'erreur |
|---|---|---|
| `PP9V8_SOC_FIXED_S1` | `PP0V8_SOC_FIXED_S1` | OCR confusion `0` ↔ `9` |
| `PPIV2_SOC` | `PP1V2_SOC` | OCR confusion `1` ↔ `I` |
| `VDD_FIXED_PLL_CPU` | autre net (probable confusion CPU/GPU) | erreur sémantique |

### Mauvaise classification (Sonnet)

61 « connectors » et 3 « fuses » émis par Sonnet sur cette page :
- Les refdes (`H11`...`H125`, `F22`/`F25`/`F31`) existent dans le PDF → pas inventés.
- MAIS la classification est fausse : `H` en convention Apple = mounting holes mécaniques, `F` = fiducials / test-points sur certaines pages. Sonnet les promeut tous en composants électriques, ce qui pollue le graph compilé.

### Coûts (single-page test)

| Modèle | Tokens (in/out/cache) | Coût | Wall |
|---|---|---|---|
| Opus 4.7 (estimé pour mêmes tokens shape) | — | $0.46 | ~60 s |
| Sonnet 4.6 | 12 468 / 14 878 / 4 627 cache | $0.28 | 148 s |

Sonnet ~40 % moins cher mais ~2,5× plus lent.

## Pourquoi c'est rédhibitoire pour Sonnet

Une OCR error sur un nom de rail **casse silencieusement le power tree** :
- Si le compiler reçoit `PP9V8_SOC_FIXED_S1` au lieu de `PP0V8_SOC_FIXED_S1`, tous les downstream consumers du vrai rail perdent leur lien à la source.
- Le rail apparaît comme « sourceless » → le simulator le présume always-on (cf. `simulator.py:197`).
- Le diagnostic devient muet sur cette branche du power tree, sans aucun warning.

Sur 21 pages avec un taux de 3 hallucinations / 30 nets = ~10 %, on aurait potentiellement **15-20 noms de rails corrompus** sur l'ElectricalGraph compilé. La cascade physique est subtilement fausse, INV-3 et INV-4 ne le détectent pas (les refdes existent, juste mal nommés).

## Décision

**Garder Opus 4.7 comme modèle par défaut sur le vision pipeline.** L'économie de 40 % via Sonnet n'est pas acceptable au prix de la corruption silencieuse du power tree.

## Quand reconsidérer

- Sortie d'un nouveau modèle vision Anthropic (Sonnet 4.7+, Haiku avec vision améliorée, etc.) → re-tester avec la même méthodologie.
- Si on veut quand même réduire le coût Opus :
  - **Pré-classification** : Haiku 4.5 trie les pages par pertinence (power tree vs feuilles terminales), Opus n'analyse que les pages utiles. Déjà appliqué à la main pour iPhone X (21/87 pages).
  - **Sonnet + audit Haiku** : Sonnet extrait, Haiku 4.5 vérifie chaque refdes/net contre le PNG re-rendu. Coût Sonnet $0.28 + Haiku ~$0.05/page = $0.33/page vs Opus $0.46. Économie ~28 % sans hallucination, mais nécessite du code en plus dans la pipeline.
  - **Cache de prompt système** : la pipeline ne profite pas du cache (pages 2-N partent en parallèle avant que page 1 chauffe le cache). Pourrait économiser ~30 % sur tout modèle.

Ne PAS migrer la pipeline à Sonnet 4.6 sans nouvelle preuve empirique.

## Ground-truth check (script à reproduire)

```python
import json, pdfplumber
PDF = "<path-to-pdf>"
PAGE = <0-indexed-page>
extracted = json.load(open("<page_NNN.json>"))

with pdfplumber.open(PDF) as pdf:
    text = pdf.pages[PAGE].extract_text() or ""

missing_refdes = [n.get("refdes") for n in extracted["nodes"]
                  if n.get("refdes") and n["refdes"] not in text]
missing_nets = [n["label"] for n in extracted["nets"]
                if n["label"] not in text and n["label"] != "GND"]
print(f"refdes inventés: {missing_refdes}")
print(f"nets inventés: {missing_nets}")
```

Toute hallucination > 1 % sur les nets disqualifie le modèle pour ce pipeline.
