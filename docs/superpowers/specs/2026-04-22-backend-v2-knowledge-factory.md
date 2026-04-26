# Spécification Architecture Backend V2 : "Prod-Ready Knowledge Factory"

**Date :** 22 Avril 2026
**Projet :** `wrench-board` (Hackathon Anthropic x Cerebral Valley)
**Mode d'Orchestration :** Asynchrone Multi-Agents (Swarm)
**Technologie :** Python, FastAPI, Anthropic SDK (Opus 4.7)

---

## 1. Vision et Paradigme

Nous basculons d'une architecture "Document-Centric" (dépendante d'un PDF officiel) vers une architecture **"Knowledge-Centric"**.
Le backend orchestre un essaim (Swarm) d'agents IA capables de rechercher, structurer et auditer les pannes fréquentes d'un appareil électronique depuis les communautés de réparation (iFixit, forums, wikis).

L'objectif est de générer une **Memory Bank déterministe** (JSON) qui servira de cerveau à l'Agent Diagnostic final, et de base de données visuelle pour le Frontend (Knowledge Graph).

---

## 2. Le Pipeline Multi-Agents (Les 4 Phases)

L'orchestrateur FastAPI (`api/pipeline/orchestrator.py`) exécute les 4 phases de manière asynchrone.

### Phase 1 : L'Agent "Scout" (Recherche Autonome)
* **Rôle :** Chercheur Web et Extracteur de données brutes.
* **Outil :** `web_search_20250305` (API native Anthropic).
* **Contrainte de Prompt :** Doit utiliser l'opérateur `site:` (ex: `site:repair.wiki`, `site:badcaps.net`) pour exclure le bruit SEO grand public.
* **Output attendu :** Un fichier texte structuré `raw_research_dump.md` contenant les symptômes majeurs, les composants suspectés, les signaux (nets) impliqués et les sources URL. **Aucun JSON généré à cette étape.**

### Phase 2 : Le "Registry Builder" (L'Ancre de Vérité)
* **Rôle :** Figer le vocabulaire autorisé pour empêcher le "Data Drift" et les hallucinations.
* **Input :** `raw_research_dump.md`.
* **Output attendu :** `registry.json`.
* **Contrainte technique :** Doit utiliser un schéma Pydantic strict pour forcer la sortie (Tool Use). Il liste tous les composants avec leur `canonical_name` ou un `logical_alias` en cas de doute.

### Phase 3 : Le Swarm des "Writers" (Génération Parallèle)
Trois agents Opus 4.7 travaillent en parallèle via `asyncio.gather()`.
**Règle d'Optimisation Financière (Prompt Caching) :** Les 3 agents reçoivent *exactement* le même préfixe de prompt (Le Dump + Le Registre + `cache_control: {"type": "ephemeral"}`). L'orchestrateur lance le premier agent, effectue un `asyncio.sleep(1)` pour permettre l'écriture du cache chez Anthropic, puis lance les deux autres.

1. **Le Cartographe (Architecture Writer) :**
   * Produit : `knowledge_graph.json`
   * Structure : Liste de `nodes` (type: component, symptom, net) et d'`edges` (relation: causes, powers, connected_to).
2. **Le Clinicien (Rules Writer) :**
   * Produit : `rules.json`
   * Structure : Arbre de diagnostic (Symptôme -> Causes probables avec probabilités -> Étapes de diagnostic).
3. **Le Lexicographe (Dictionary Writer) :**
   * Produit : `dictionary.json`
   * Structure : Fiches d'identité techniques de chaque composant. *Contrainte forte : Ne doit jamais rien deviner. Si une info (ex: le package) manque, il doit renvoyer `null`.*

**Règle absolue de la Phase 3 :** Chaque Writer NE DOIT utiliser QUE les composants listés dans le `registry.json`.

### Phase 4 : L'Auditeur (Contrôle Qualité & Self-Healing)
* **Rôle :** Vérifier que les 3 Writers n'ont pas halluciné.
* **Inputs :** Le Registre + Les 3 JSON générés (Graph, Rules, Dictionary).
* **Output attendu :** `audit_verdict.json`. Contient un statut (`APPROVED` ou `NEEDS_REVISION`) et un tableau d'erreurs (`drift_report`).
* **Logique de Correction Automatique (FastAPI) :** Si le verdict est `NEEDS_REVISION`, l'orchestrateur lit les `files_to_rewrite`, relance le Writer concerné en lui injectant le `revision_brief` de l'Auditeur, puis resoumet le résultat à l'Auditeur (limite : 1 à 2 boucles maximum pour préserver le budget).

---

## 3. Contrats de Données (Pydantic V2)

Pour garantir la solidité du pipeline, toutes les sorties d'agents DOIVENT être forcées via des outils Anthropic ("Tool Use") définis par des schémas Pydantic.

### Exemple : Le Knowledge Graph (Sortie du Cartographe)

```python
from pydantic import BaseModel, Field
from typing import Literal

class GraphNode(BaseModel):
    id: str
    type: Literal["component", "symptom", "net", "action"]
    label: str
    description: str | None
    confidence: float

class GraphEdge(BaseModel):
    source: str
    target: str
    relation: Literal["causes", "powers", "connected_to", "resolves"]
    label: str
    weight: float

class KnowledgeGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
```

> **WIP — sections restant à documenter** :
> - §3 : schémas Pydantic restants (Registry, RulesSet, Dictionary, AuditVerdict).
>   Source de vérité actuelle : `api/pipeline/schemas.py`.
> - §4 : Layout disque des knowledge packs (`memory/{slug}/`) et invariants (`raw_research_dump.md`, `registry.json`, `knowledge_graph.json`, `rules.json`, `dictionary.json`, `audit_verdict.json`).
> - §5 : Contrat Frontend — endpoints `GET /pipeline/packs`, `GET /pipeline/packs/{slug}`, `GET /pipeline/packs/{slug}/graph` et les shapes qu'ils retournent.
> - §6 : Règles de résilience (validation Pydantic sur tool output, retry avec `max_attempts=2`, `cache_warmup_seconds`, `pipeline_max_revise_rounds`).
