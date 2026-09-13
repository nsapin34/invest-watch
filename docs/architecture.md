# Architecture — invest-watch

> Fondé sur les spécifications fonctionnelles et techniques v1.3 (2026-09-08).
> Décisions DA-01 à DA-07 arbitrées et intégrées — voir §7 et §A.

---

## 1. Résumé des exigences

### Exigences fonctionnelles

| Ref | Exigence |
|---|---|
| EF-01 | Collecte quotidienne automatique (07h00) des articles publiés la veille (J-1) sur les compagnies du portefeuille |
| EF-02 | Gestion CRUD des compagnies (max 50), marchés/secteurs/industries, sources et alias via CLI |
| EF-03 | Cascade de sources : RSS éditoriaux → Google News RSS → DuckDuckGo |
| EF-04 | Filtrage par date (J-1 ± 6 h) dans le fuseau utilisateur (par défaut `Europe/Paris`) |
| EF-05 | Extraction et nettoyage du texte des pages (`trafilatura`, `httpx`, respect de `robots.txt`) |
| EF-06 | Détection de langue (`lingua`) et de date (`dateparser` + JSON-LD / meta) |
| EF-07 | Classification des articles par compagnie, marché et catégorie (règles + enrichissement LLM optionnel) |
| EF-08 | Déduplication en 4 couches (URL normalisée, SHA-256, Jaccard titres, MinHash contenu) + regroupement d'événements |
| EF-09 | Résumés d'articles (JSON structuré) et digests quotidiens (compagnie / marché / global) via LLM IBM watsonx.ai |
| EF-10 | Mode dégradé complet si LLM indisponible (extraits heuristiques, workflow ininterrompu) |
| EF-11 | Liens courts locaux (code 6-8 caractères) tracés jusqu'à l'URL originale |
| EF-12 | Rapports quotidiens `reports/YYYY-MM-DD.md` et `reports/YYYY-MM-DD.json` |
| EF-13 | Purge automatique des contenus textuels à 90 jours ; sauvegarde SQLite quotidienne (`VACUUM INTO`) |
| EF-14 | CLI complète : `invest-watch run/report/company/market/status/errors/export/search/source/db` |
| EF-15 | Fonctions avancées V1+ : calendrier catalyseurs (F22), détection momentum (F23), vérification croisée chiffres (F24), thèse d'investissement (F25), drivers macro (F26), sentiment 30-90 j (F27) |
| EF-16 | Interface web locale (V2) sur `127.0.0.1` pour consultation et édition |

### Exigences non fonctionnelles

| Ref | Exigence |
|---|---|
| ENF-01 | **Résilience** : tout service optionnel indisponible (LLM, source, moteur) déclenche le mode dégradé — l'application ne s'arrête jamais |
| ENF-02 | **Idempotence** : chaque étape du workflow est répétable sans doublon (upserts sous contraintes d'unicité) |
| ENF-03 | **Durées max** : 90 min par run global ; 30 s/page ; 10 s/flux RSS |
| ENF-04 | **Conformité réseau** : respect de `robots.txt`, User-Agent identifié `invest-watch/1.0`, débit ≤ 0,5 req/s/hôte, 40 requêtes moteur max/run |
| ENF-05 | **Sécurité des secrets** : clés API jamais en clair dans config, logs ou rapports ; `.env` hors git (permissions 700) |
| ENF-06 | **Traçabilité** : chaque fait lié à un `#id` → article → URL originale |
| ENF-07 | **Portabilité** : Windows (cible principale V1), Linux/macOS documentés |
| ENF-08 | **Testabilité** : tous les tests hors ligne sans clé API ; couverture `pytest-cov` ≥ 80 % |
| ENF-09 | **Évolutivité** : architecture modulaire permettant l'ajout V2 (`web_ui`, `insight_engine`) sans refonte du cœur |
| ENF-10 | **Volume maîtrisé** : ≤ 50 compagnies ; 40 requêtes/run ; 150 appels LLM/run ; réponses > 2 Mo rejetées |

---

## 2. Ambiguïtés et questions en suspens

Toutes les ambiguïtés ci-dessous ont été tranchées. Les décisions sont intégrées au §7 (DA-08 à DA-11).

| # | Ambiguïté | Décision ✔ |
|---|---|---|
| A1 ✔ | **D8 — Modèle watsonx.ai** | Modèle sélectionnable via `.env` (`WATSONX_MODEL_ID`) ; aucune valeur par défaut codée en dur — voir DA-01 |
| A2 ✔ | **D9 — Niveau de compte watsonx.ai** | Quota traité comme illimité ; aucune borne de coût bloquante — voir DA-02 |
| A3 ✔ | **D19 — Forme de l'interface V2** | Interface web FastAPI locale liée à `127.0.0.1` — voir DA-03 |
| A4 ✔ | **Brave Search gratuit** | Brave Search inclus dès V1 ; tier gratuit confirmé à 1 000 req/mois — voir DA-08 |
| A5 ✔ | **Flux RSS des sources chinoises (NBS, PBoC, CSRC)** | Mode `type = 'website'` avec scraping de page via `fetcher` (`httpx` + `trafilatura`) — voir DA-04 |
| A6 ✔ | **F22 — Alimentation du calendrier des catalyseurs** | Extraction automatique par regex + LLM suffisante ; pas d'interface de saisie manuelle à prévoir — voir DA-09 |
| A7 ✔ | **Reprise d'un run LLM partiellement facturé** | Pas de reprise `--resume` prévue ; cache RAM par run uniquement ; re-traitement au run suivant acceptable — voir DA-10 |
| A8 ✔ | **Synchronisation FTS5 et purge** | Tâche de fond séparée acceptable ; la purge n'a pas besoin d'être synchrone avec la transaction principale — voir DA-11 |

---

## 3. Architecture proposée

L'architecture est **modulaire, locale, sans serveur**, centrée sur un fichier SQLite unique. Le planificateur OS déclenche la CLI qui orchestre un pipeline de modules indépendants, tous faillibles individuellement.

```
Planificateur OS (07h00)
        │
        ▼
  ┌──── CLI (invest) ─────────────────────────────────────────────────┐
  │  argparse : run, report, company, market, status, errors, export, │
  │             search, source, db, catalysts, momentum, thesis, macro │
  └───────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
  ┌───────────────────── ORCHESTRATEUR ───────────────────────────────┐
  │  Verrou run.lock · daily_run lifecycle · Workflow §10 (15 étapes) │
  │  Reprise --resume · Arrêt propre stop.flag · Stats + rapport      │
  └──┬──────┬──────┬────────┬────────┬────────┬───────┬──────────────┘
     │      │      │        │        │        │       │
  SEARCH  FETCH  CLEAN  CLASSIFY   DEDUP  SUMMARIZE SHORT_LINK
  (RSS/   (http+  (trafi-  (règles+  (4     (LLM ou   (codes
  GNews/  robots) latura)  LLM opt.) couch.) dégradé)  courts)
   DDG)
     │      │      │        │        │        │       │
     └──────┴──────┴────────┴────────┴────────┴───────┘
                             │
               ┌─────────────┴────────────┐
               │        DB_MANAGER        │  data/invest.db
               │  SQLite WAL · FTS5       │  migrations versionnées
               │  repositories · purge    │  backup VACUUM INTO
               └──────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
   INSIGHT_ENGINE (V1+)   WEB_UI (V2)       CONF · LOGGER · ERROR_MGR
   F22–F27                127.0.0.1
```

**Principes architecturaux :**

1. **Aucun module n'écrit directement en base** : tout passe par `db_manager` (repositories) ; chaque insertion est rattachée à un `daily_run`.
2. **Tous les appels réseau passent par `fetcher` / `search_engine`** : débit global géré par jeton, User-Agent unique, timeouts.
3. **`llm_client` est un port** : implémentation watsonx.ai, stub de test, et mode `off` (dégradé) sans la moindre dépendance.
4. **Chaque étape du workflow est idempotente** : upsert sous contraintes d'unicité — la reprise ne duplique rien.
5. **Échec partiel toléré** : toute exception dans un module est capturée, enregistrée dans `processing_error`, et le run continue avec les sources restantes.

---

## 4. Composants, responsabilités et interfaces

| Module | Responsabilité | Entrées | Sorties | Dépendances clés |
|---|---|---|---|---|
| `config_manager` | Charge `conf/config.toml`, résout `${VAR}` depuis env / `.env` ; produit une config immuable | TOML + variables d'environnement | Objet `AppConfig` (lecture seule) | `tomllib`, `python-dotenv` |
| `cli` | Point d'entrée utilisateur ; dispatch des commandes vers orchestrateur ou repositories | Arguments `sys.argv` | Appels orchestrateur / repositories, affichage terminal | `argparse`, tous les repositories |
| `orchestrator` | Enchaîne les 15 étapes du workflow ; gère `daily_run`, verrou, reprise, arrêt propre | Config + listes compagnies/marchés actifs | `daily_run` mis à jour, rapport écrit sur disque | Tous les modules |
| `company_repository` | CRUD compagnies, alias, marchés associés ; vecteurs de recherche (nom, ticker, ISIN, site) | Commandes CLI / orchestrateur | Objets `Company`, listes paginées | `db_manager` |
| `market_repository` | CRUD marchés/secteurs/industries ; hiérarchie `parent_id` ; taxonomie GICS | Commandes CLI / orchestrateur | Objets `Market`, arbres hiérarchiques | `db_manager` |
| `source_repository` | CRUD sources ; validation ping HTTP/RSS ; métriques `last_success_at` / `error_count` | Commandes CLI / orchestrateur | Objets `Source` | `db_manager`, `httpx` |
| `search_engine` | Génère les requêtes (3/compagnie + 2/marché, plafond 40/run) ; cascade RSS → Google News RSS → DDG ; limite de débit avec jitter | Listes `Company` + `Market` + config sources | Candidats bruts `(titre, url, date_str, source_id)` | `feedparser`, `httpx`, `duckduckgo-search` |
| `fetcher` | Récupération HTTP polie ; respect `robots.txt` (cache par hôte) ; capture URL finale après redirection ; taille bornée à 2 Mo | URL candidates | `(html_content, url_final, status_code)` | `httpx`, `urllib.robotparser` |
| `cleaner` | HTML → texte principal (`trafilatura`) ; suppression boilerplate ; extrait 400 car. ; normalisation espaces | HTML brut | `(text, excerpt, word_count)` | `trafilatura`, `beautifulsoup4` |
| `date_detector` | Date publication depuis JSON-LD, meta tags, RSS feed, puis `dateparser` ; flag `date_certaine` | HTML + métadonnées RSS + fuseau config | `(published_at UTC, date_certaine bool)` | `dateparser`, `trafilatura` |
| `lang_detector` | Détection langue sur 300 premiers caractères ; repli `langdetect` | Texte (300 car.) | Code langue BCP-47 | `lingua-language-detector`, `langdetect` |
| `classifier` | Associe compagnies/marchés/catégories par règles (alias, mots-clés) ; calcule `relevance_score` et `importance_level` ; enrichissement LLM optionnel si confiance faible | Texte + métadonnées + listes config | Junctions `article_company/market/category` + scores | `db_manager`, `llm_client` (opt.) |
| `dedup` | Couche 1 : normalisation URL (suppression `utm_*`, fragment) ; Couche 2 : SHA-256 du texte normalisé ; Couche 3 : Jaccard titres ≥ 0,85 ; Couche 4 : MinHash ≥ 0,9 ; Couche 5 : regroupement d'événements (même compagnie, ±24 h, similarité ≥ seuil) | Article courant + articles 7 derniers jours en mémoire | `(status, event_id, dedup_reason)` | `datasketch`, `db_manager` |
| `llm_client` | Interface (port) d'appel watsonx.ai : auth IAM, construction des prompts, parsing JSON, validation schéma, timeouts, retries (3×), circuit-breaker (3 échecs consécutifs → dégradé), compteur d'appels | Texte tronqué + contexte (compagnies/marchés) | JSON structuré validé ou `LLMDegradedError` | `ibm-watsonx-ai` SDK |
| `summarizer` | Résumés d'articles (JSON structuré) ; digests compagnie / marché / global ; mode dégradé (extrait de tête + faits heuristiques) ; cache par `content_hash` | Articles pertinents + résumés existants | `summary` records (type `article/company/market/global`) | `llm_client`, `db_manager` |
| `short_link` | Génère un code court aléatoire sécurisé (6-8 car., `secrets.token_urlsafe`) avec unicité en base ; rendu `[code](url)` Markdown | URL originale + `article_id` | `short_code` TEXT | `db_manager`, `secrets` stdlib |
| `db_manager` | Connexion SQLite WAL ; migrations versionnées (`app_meta.db_version`) ; repositories ; FTS5 `article_fts` ; purge `content` à 90 j ; backup `VACUUM INTO` | SQL / objets Python | Curseurs / résultats / transactions | `sqlite3` stdlib |
| `logger` | Logs horodatés UTC ; rotation par taille ; fichier dédié `logs/run-YYYY-MM-DD.log` par exécution ; niveaux configurables | Événements applicatifs | `logs/invest.log`, `logs/run-DATE.log` | `logging` stdlib |
| `error_manager` | Enregistre `processing_error` ; backoff exponentiel (2 s → 8 s → 30 s) ; isolation par source ; circuit-breaker LLM ; statuts `new/retrying/failed/resolved` | Exceptions + contexte (step, source_id, article_id) | `processing_error` en base ; décision retry/skip | `db_manager` |
| `insight_engine` **(V1+)** | Calendrier catalyseurs (F22) ; détection momentum (F23) ; fact-check chiffres (F24) ; thèse d'investissement (F25) ; drivers macro (F26) ; sentiment 30-90 j (F27) | Articles/résumés/mentions existants + config | Tables `catalyst_event`, `mention_stats`, `macro_indicator`, `sentiment_history` ; sections rapport | `db_manager`, `llm_client` (opt.) |
| `web_ui` **(V2)** | Serveur web local `127.0.0.1` ; consultation nouvelles/résumés/marchés ; édition compagnies/marchés ; déclenchement run ; toutes les écritures via repositories | Requêtes HTTP locales | Pages HTML / JSON ; déclenchement orchestrateur | `FastAPI` ou `Flask`, tous les repositories, orchestrateur |

---

## 5. Modèle de données et flux principaux

### Schéma entité-relation (tables principales)

```
┌──────────────┐        ┌──────────────────┐        ┌──────────────┐
│   company    │───┐    │  company_market   │────────│   market     │
│  id, name,   │   └───▶│  company_id FK    │        │  id, name,   │
│  ticker, isin│        │  market_id FK     │        │  type, parent│
│  country, ... │        │  weight           │        └──────────────┘
└──────┬───────┘        └──────────────────┘
       │
       │  ┌───────────────────────────────────────────────────────────┐
       │  │                       article                             │
       │  │  id, run_id FK, source_id FK, url_original, url_normalized│
       │  │  title, language, published_at (UTC), date_certaine       │
       │  │  content (purge 90j), excerpt, content_hash, title_hash   │
       │  │  short_code, status, event_id FK (self), relevance_score  │
       │  │  importance_level, requires_human_check, update_count     │
       │  └────────┬──────────────────────────────────┬──────────────┘
       │           │                                  │
       │  ┌────────▼────────┐              ┌──────────▼──────────┐
       │  │ article_company │              │  article_market      │
       │  │ article_id FK   │              │  article_id FK       │
       │  │ company_id FK   │              │  market_id FK        │
       │  │ relevance, ...  │              │  relevance, ...      │
       │  └─────────────────┘              └─────────────────────┘
       │
       │  ┌───────────────┐   ┌────────────────┐   ┌────────────────┐
       └─▶│ catalyst_event│   │  mention_stats  │   │sentiment_history│
          │ F22           │   │  F23            │   │  F27           │
          └───────────────┘   └────────────────┘   └────────────────┘

┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  daily_run   │   │   summary    │   │processing_err│
│  run_date,   │──▶│  type, ref_id│   │  step, source│
│  status, stats│  │  content (MD)│   │  retry_count │
│  llm_status  │  │  json_payload│   │  status      │
└──────────────┘  │  model_id,   │   └──────────────┘
                  │  llm_used    │
                  └──────────────┘

┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  short_link  │   │article_version│  │macro_indicator│
│  short_code  │   │  article_id  │  │  F26          │
│  url_original│   │  content_hash│  │  indicator_key│
│  article_id  │   │  changed_at  │  │  value, period│
└──────────────┘   └──────────────┘   └──────────────┘
```

### Flux principal — workflow quotidien

```
Planificateur → CLI → Orchestrateur
    │
    ├─ 1. Calculer période J-1 (fuseau configuré → UTC) → period_start / period_end
    ├─ 2. Charger config (compagnies, marchés, sources actifs)
    ├─ 3. Générer requêtes (3/compagnie + 2/marché, plafond 40)
    │
    ├─ 4. Interroger sources ──▶ [JAMAIS BLOQUANT]
    │       RSS éditoriaux → Google News RSS → DuckDuckGo
    │       → candidats bruts (titre, url, date, source_id)
    │
    ├─ 5. Filtrer par date (J-1 ± 6 h, date_certaine)
    ├─ 6. Extraire contenus (fetcher + trafilatura)
    ├─ 7. Nettoyer texte (normalisation, hash SHA-256)
    ├─ 8. Dédupliquer (4 couches vs base 7 j + intra-run)
    ├─ 9. Classifier (langue, catégories, compagnies/marchés, scores)
    ├─ 10. Évaluer importance (règles : catégorie + autorité + amplitude)
    │
    ├─ 11. Résumer ──▶ [CIRCUIT-BREAKER]
    │        LLM watsonx.ai (filtre relevance ≥ 0.5, budget 150 appels, cache hash)
    │        → mode dégradé si indisponible
    │
    ├─ 12. Enregistrer (upserts idempotents SQLite)
    ├─ 13. Digests (compagnie → marché → global)
    ├─ 14. Générer rapport (reports/YYYY-MM-DD.md + .json)
    └─ 15. Bilan (stats_json, statut completed/partial, purge 90j, backup)
```

### Flux de déduplication

```
Article entrant
    │
    ├─ Couche 1 : URL normalisée (suppression utm_*, fragment, lowercase)
    │              → doublon exact ? → status = 'duplicate', FK vers original
    │
    ├─ Couche 2 : SHA-256(texte normalisé)
    │              → hash identique ? → status = 'duplicate'
    │
    ├─ Couche 3 : Jaccard(tokens titre) ≥ 0.85
    │              → même dépêche ? → status = 'duplicate'
    │
    ├─ Couche 4 : MinHash(contenu, 128 permutations) ≥ 0.9
    │              → quasi-reprise ? → status = 'redundant'
    │              sauf si source rang 1-2 ou diff > 10 % → 'complementary'
    │
    └─ Couche 5 : Regroupement événements
                   même compagnie + fenêtre ±24 h + similarité ≥ seuil
                   → premier = tête (event_id = self.id)
                   → suivants rattachés (event_id = tête.id)
                   nouveau si aucun cluster 14 derniers jours
```

---

## 6. Risques identifiés

### Sécurité

| Risque | Criticité | Mitigation |
|---|---|---|
| Exposition de la clé API watsonx.ai | **Haute** | `.env` hors git (permissions 700) ; jamais dans logs, rapports, `config.toml` ; rotation documentée ; trousseau OS (`keyring`) en alternative |
| Injection dans les prompts LLM via contenu web | **Moyenne** | Templates sanitisés ; texte tronqué à 8 000 car. ; prompts factuels (`ne rien inventer`) ; sortie JSON validée par schéma strict |
| Exposition de données du portefeuille via V2 web UI | **Moyenne** | Serveur lié à `127.0.0.1` uniquement ; aucune API publique ; toutes les écritures via repositories ; journalisation des actions |
| Vol ou lecture non autorisée du fichier `invest.db` | **Moyenne** | Droits de fichier restreints à l'utilisateur OS ; option SQLCipher (V3) ; purge à 90 jours limite l'exposition historique |
| Exfiltration de données via les textes envoyés au LLM | **Faible** | Données minimales envoyées (titre, date, source, texte tronqué, candidats) ; jamais de clés, chemins, identifiants internes, ou contenus hors budget |

### Performance

| Risque | Criticité | Mitigation |
|---|---|---|
| Dépassement de la durée max (90 min) avec 50 compagnies | **Moyenne** | Quotas internes (40 requêtes, 30 s/page, 10 s/flux) ; timeout global ; run marqué `partial` si dépassé |
| Explosion du volume de la base SQLite | **Moyenne** | Purge `content` à 90 jours ; FTS5 synchronisée à la purge ; `VACUUM INTO` quotidien ; `max_response_bytes = 2 Mo` |
| Blocage IP (DuckDuckGo, agrégateurs) | **Moyenne** | Débit poli + jitter aléatoire ; volumes faibles ; `use_search_engines = false` désactive le scraping |
| Coût LLM inattendu (tokens / appels) | **Moyenne** | Filtre `relevance_score ≥ 0.5` ; cache par `content_hash` (aucun re-appel) ; borne `max_calls_per_run = 150` ; circuit-breaker ; métriques dans le rapport |
| Ralentissement sur textes chinois (lingua, dateparser) | **Faible** | Détection `lingua` sur 300 car. uniquement ; repli `langdetect` plus rapide ; `dateparser` paramétré avec le fuseau courant |

### Maintenance

| Risque | Criticité | Mitigation |
|---|---|---|
| Disparition ou évolution des sources gratuites (Google News RSS, DDG, Brave) | **Élevée** | Cascade de sources configurables ; `invest-watch sources validate` détecte les flux morts ; sources en base éditable sans redéploiement |
| Dérive du schéma SQLite lors des évolutions | **Moyenne** | Migrations versionnées SQL (`app_meta.db_version`) ; tests de migration (`v1 → v2`) ; backup automatique avant montée de schéma |
| Évolution de l'API / SDK watsonx.ai | **Moyenne** | `llm_client` est un port (interface) — l'implémentation watsonx.ai est swappable sans toucher le reste du code |
| Hallucinations LLM non détectées dans les rapports | **Haute** | Prompts factuels (`ne rien inventer`) ; schéma JSON strict avec bornes et énumérations ; références `#id` obligatoires ; section « non vérifiées » ; relecture humaine des critiques |
| Paywalls et contenus dynamiques inaccessibles | **Élevée** | Métadonnées seules conservées (titre, extrait OpenGraph) ; pas de navigateur headless en V1 ; fixtures de test couvrant ces cas |
| Reprise corrompue après crash | **Faible** | Verrou `run.lock` + idempotence des upserts + backup quotidien ; `--resume` rejoue uniquement les erreurs `new` / `retrying` |

---

## 7. Décisions d'architecture — arbitrées ✔

Toutes les décisions ci-dessous ont été validées et sont intégrées dans les sections §4, §8 et l'annexe §A.

| # | Décision | Choix retenu | Conséquences sur l'implémentation |
|---|---|---|---|
| DA-01 ✔ | **SDK watsonx.ai + modèle + région** | Intégrer `ibm-watsonx-ai` via `ModelInference` ; modèle et URL région sélectionnables dans `.env` (`WATSONX_MODEL_ID`, `WATSONX_URL`) | Voir §A — intégration SDK complète avec exemple de code, liste des régions et pattern `.env` |
| DA-02 ✔ | **Budget LLM** | **Illimité** — pas de borne `max_calls_per_run` bloquante ; les métriques d'appels restent tracées dans `daily_run.stats_json` | Supprimer la borne de coût dans `[llm]` ; conserver `max_calls_per_run` comme information uniquement (non bloquant) |
| DA-03 ✔ | **Interface graphique V2** | **Web locale FastAPI** liée à `127.0.0.1` | Dépendance `fastapi` + `uvicorn` ; tests via `httpx.AsyncClient(app=app)` ; `invest-watch gui` lance `uvicorn` |
| DA-04 ✔ | **Sources chinoises sans RSS** | `type = 'website'` dans table `source` ; `fetcher` standard (`httpx` + `trafilatura`) pour NBS, PBoC, CSRC | Ajouter `type = 'website'` à l'enum `CHECK` de `source.type` ; `search_engine` sait appeler `fetcher` directement pour ces sources |
| DA-05 ✔ | **Cache LLM** | **Cache mémoire par run uniquement** — dict `{content_hash: summary_json}` en RAM, non persisté | Pas de ré-appel intra-run ; pas de reprise `--resume` prévue (voir DA-10) ; les articles non résumés sont re-traités au run suivant |
| DA-06 ✔ | **Purge FTS5** | **Tâche de fond séparée** (asynchrone) — non synchrone avec la transaction principale | `purge_fts5()` appelée à l'étape 15 dans un thread séparé ; le workflow principal n'est pas bloqué |
| DA-07 ✔ | **Tests des sources institutionnelles** | Fixtures statiques `tests/fixtures/` + `httpx-mock` ; CI entièrement hors ligne | Créer les dossiers `tests/fixtures/rss/`, `tests/fixtures/html/` avec exemples NBS, PBoC, HKEXnews, SEC EDGAR |
| DA-08 ✔ | **Brave Search** | Inclus dès V1 ; tier gratuit confirmé à 1 000 req/mois ; clé API dans `.env` (`BRAVE_API_KEY`) | Ajouter `brave` comme type de source `search` dans la cascade (après Google News RSS, avant DuckDuckGo) ; `search_engine` appelle l'API Brave Search avec `BRAVE_API_KEY` |
| DA-09 ✔ | **Alimentation calendrier catalyseurs F22** | Extraction automatique par regex + LLM uniquement ; pas d'interface de saisie manuelle | `insight_engine` gère l'extraction ; pas de formulaire CLI dédié à développer pour l'étape 8 |
| DA-10 ✔ | **Reprise de run LLM (`--resume`)** | Pas de `--resume` prévu ; cache RAM par run uniquement ; re-traitement au run suivant acceptable | Supprimer `--resume` du périmètre ; simplifie `orchestrator` et `llm_client` ; DA-05 mis à jour en conséquence |
| DA-11 ✔ | **Mode de purge FTS5** | Tâche de fond séparée (asynchrone) | `purge_fts5()` s'exécute dans un thread à l'étape 15 ; ne bloque pas la transaction principale ; DA-06 mis à jour en conséquence |

---

## 8. Plan d'implémentation par étapes

Chaque étape est **indépendante, testable et livrable** sans bloquer la suivante. L'ordre suit les dépendances techniques ascendantes.

---

### Étape 1 — Socle : configuration, base de données, CLI de base

**Périmètre** : `config_manager`, `db_manager` (schéma initial + migrations versionnées), `logger`, `cli` (squelette `argparse`), `company_repository`, `market_repository`, `source_repository`.

**Tests** :
- Chargement TOML valide / invalide / secrets manquants
- Création du schéma SQLite en mémoire (`sqlite3:///:memory:`)
- CRUD compagnies, marchés, sources (unicité, contraintes, hiérarchie `parent_id`)
- Migration `v1 → v2` idempotente, `app_meta.db_version` mis à jour, données préservées

**Livrable** : `invest-watch company add/list/edit/remove`, `invest-watch market …`, `invest-watch source …`, `invest-watch db migrate` fonctionnels.

---

### Étape 2 — Collecte : recherche, extraction et nettoyage

**Périmètre** : `search_engine` (RSS + Google News RSS + DuckDuckGo), `fetcher` (httpx + robots.txt), `cleaner` (trafilatura), `date_detector`, `lang_detector`.

**Tests** :
- Fixtures HTML variées : multilingues, paywall partiel, contenu dynamique, 404, 429, réponse > 2 Mo
- Filtrage par date : dates explicites, relatives, fuseaux multiples, tolérance ±6 h, `date_certaine` correct
- Respect `robots.txt` : cache par hôte, chemins interdits rejetés
- Rate limiting : jitter + débit respecté sur les appels simulés

**Livrable** : `invest-watch run` produit une liste de candidats bruts en base (sans traitement ni rapport).

---

### Étape 3 — Traitement : classification, déduplication, liens courts

**Périmètre** : `classifier` (règles d'association compagnies/marchés/catégories, scores), `dedup` (4 couches + regroupement d'événements), `short_link`.

**Tests** :
- Table de vérité déduplication : 10 articles (4 doublons URL/hash, 3 titres quasi-identiques, 1 quasi-reprise MinHash, 2 nouveaux) → statuts exacts attendus
- Regroupement d'événements : même compagnie ±24 h → `event_id` correct
- Codes courts uniques (6-8 car., `secrets.token_urlsafe`), rendu `[code](url)` conforme

**Livrable** : articles en base avec statuts `new/duplicate/redundant/updated`, junctions `article_company/market/category`, codes courts.

---

### Étape 4 — LLM : client watsonx.ai et mode dégradé

**Périmètre** : `llm_client` (interface + implémentation watsonx.ai + stub de test + mode `off`), `error_manager` (circuit-breaker, backoff exponentiel, isolation par source).

**Tests** :
- Stub LLM : tous les tests hors ligne, sans clé API
- Timeout connexion/total, 5xx, quota épuisé → 3 retries → circuit-breaker activé (`llm_status = degraded`)
- JSON invalide → 1 tentative corrective → mode dégradé + erreur consignée
- Cache par `content_hash` : second appel sur même hash → pas d'appel LLM
- Mode `llm.enabled = false` : run complet sans aucun appel réseau LLM

**Livrable** : `llm_client` swappable (watsonx.ai / stub / off) ; mode dégradé produisant des extraits sans jamais planter.

---

### Étape 5 — Résumés et rapport quotidien

**Périmètre** : `summarizer` (résumés articles JSON + digests compagnie/marché/global + mode dégradé), générateur de rapport `.md` + `.json`.

**Tests** :
- Golden tests des prompts : parsing JSON, validation schéma (énumérations, bornes 0..1, longueur max)
- Réponse hors schéma → 1 tentative corrective → dégradé contrôlé
- Digests citant les `#id` mappés sur des articles existants en base
- Rapport conforme à la structure §13.2 (sections compagnie, marché, critiques, erreurs)
- `model_id` / `prompt_version` présents en pied de chaque résumé

**Livrable** : `invest-watch run` produit `reports/YYYY-MM-DD.md` + `.json` complets avec résumés et citations tracées.

---

### Étape 6 — Orchestrateur complet et résilience

**Périmètre** : `orchestrator` (workflow 15 étapes, verrou `run.lock`, anti-double-exécution, reprise `--resume`, arrêt propre via `stop.flag`, gestion `daily_run` lifecycle), purge 90 j, backup `VACUUM INTO`.

**Tests** :
- Run complet avec toutes les sources mockées : `daily_run` dans l'état attendu, rapport généré, aucun doublon
- Kill du processus en plein run → `--resume` : reprise sans doublon ni perte, verrou levé, statut cohérent
- Échec d'une source unique : run `partial`, autres sources traitées, `processing_error` peuplé
- Anti-double-exécution : second `invest-watch run` concurrent → sortie immédiate (code 0)
- Exit codes : `completed = 0`, `partial = 1`, `failed = 2`

**Livrable** : workflow bout en bout invocable par le Task Scheduler Windows (`schtasks`), critères AC3, AC4, AC13, AC16 validés.

---

### Étape 7 — CLI complète et consultation

**Périmètre** : toutes les commandes CLI restantes (`invest-watch report/status/errors/export/search`), recherche FTS5 (`invest-watch search "mot"`), affichage structuré des rapports.

**Tests** :
- Toutes les commandes testées sur une base pré-peuplée (sans run réel)
- `invest-watch search` : FTS5 retourne les articles correspondants, purge synchronise FTS5
- `invest-watch status` : état `daily_run`, disponibilité LLM, verrou
- `invest-watch export --format md/json` : fichier conforme au format §13.2
- Aucune commande n'expose les secrets dans sa sortie

**Livrable** : CLI complète §13.1 ; critères d'acceptation AC1–AC16 intégralement validés.

---

### Étape 8 — Fonctions avancées V1+ (F22–F27)

**Périmètre** : `insight_engine` — F22 calendrier catalyseurs, F23 détection momentum, F24 fact-check chiffres, F25 thèse d'investissement, F26 drivers macro, F27 sentiment 30-90 j.

**Tests** :
- **F22** : extraction de dates depuis fixtures de communiqués IR ; saisie CLI manuelle ; section `DEMAIN` dans le rapport
- **F23** : calcul `count_24h` vs `baseline_30d` ; seuils `×2.5 / ×5 / ×10` ; encadré `⚡ MOMENTUM` dans le rapport
- **F24** : stub LLM retournant un JSON `figures/discrepancies` ; bloc `⚠ ÉCARTS` si non vide
- **F25** : marquage heuristique (sans LLM) ; opt-in LLM ; `thesis_signal` dans `article_company`
- **F26** : ingestion fixtures RSS NBS / PBoC / CAAM ; upsert `macro_indicator` ; section `DRIVERS MACRO`
- **F27** : calcul `sentiment_history` depuis `summary.json_payload` ; CLI `invest-watch sentiment <company> --days 90`

**Livrable** : `invest-watch catalysts / momentum / macro / thesis / sentiment` opérationnels ; sections dédiées dans le rapport.

---

### Étape 9 — Interface web locale (V2)

**Périmètre** : `web_ui` (FastAPI ou Flask, lié à `127.0.0.1`) — consultation nouvelles/résumés/marchés par date, édition compagnies/marchés, déclenchement run, marquage `article.flagged`.

**Tests** :
- Toutes les routes testées via `httpx.AsyncClient` ou `TestClient` (client HTTP local)
- Aucune route accessible hors `127.0.0.1` (vérification de binding)
- Toutes les écritures passent par les repositories (contraintes identiques à la CLI)
- Déclenchement run : `daily_run` créé, interface reste responsive, état rafraîchi
- Aucune donnée envoyée à l'extérieur (pas de requête réseau sortante depuis l'UI)

**Livrable** : `invest-watch gui` lance le serveur et ouvre le navigateur ; édition compagnies/marchés via interface web conforme à la spécification §17.

---

*Document généré à partir de SPECIFICATIONS.md v1.3 — invest-watch.*

---

## Annexe A — Intégration du SDK ibm-watsonx-ai

> Décision DA-01 arbitrée. Cette annexe est la référence d'implémentation pour le module `llm_client`.

### A.1 Installation

```bash
pip install ibm-watsonx-ai
```

Le paquet est disponible sur PyPI et maintenu par IBM. Il fournit l'interface `ModelInference` pour les appels aux fondation models, ainsi que `APIClient` et `Credentials` pour l'authentification IAM.

Version recommandée dans `requirements.txt` :

```
ibm-watsonx-ai>=1.1.22
```

### A.2 Variables d'environnement (`.env`)

Toutes les variables de configuration watsonx.ai sont déclarées dans le fichier `.env` (hors git, permissions 700). Le modèle et la région sont ainsi **changeables sans toucher au code**.

```dotenv
# ── watsonx.ai ──────────────────────────────────────────────────────────────
# Clé API IAM IBM Cloud
WATSONX_API_KEY=your-ibm-cloud-api-key

# URL de l'endpoint régional — choisir parmi :
#   Dallas (US)   : https://us-south.ml.cloud.ibm.com
#   Frankfurt (EU): https://eu-de.ml.cloud.ibm.com   ← recommandé RGPD
#   Londres (EU)  : https://eu-gb.ml.cloud.ibm.com
#   Tokyo         : https://jp-tok.ml.cloud.ibm.com
#   Sydney        : https://au-syd.ml.cloud.ibm.com
WATSONX_URL=https://eu-de.ml.cloud.ibm.com

# Identifiant du projet watsonx.ai (ou space_id pour un déploiement)
WATSONX_PROJECT_ID=your-project-uuid

# Modèle à utiliser — exemples disponibles selon région :
#   ibm/granite-3-8b-instruct
#   ibm/granite-3-3-8b-instruct
#   meta-llama/llama-3-3-70b-instruct
# Vérifier le catalogue de votre région :
#   curl -H "Authorization: Bearer $TOKEN" \
#        "$WATSONX_URL/ml/v1/foundation_model_specs?version=2024-09-16"
WATSONX_MODEL_ID=ibm/granite-3-8b-instruct
```

Le `config_manager` résout ces variables via `${WATSONX_API_KEY}`, `${WATSONX_URL}`, `${WATSONX_PROJECT_ID}`, `${WATSONX_MODEL_ID}` — jamais en dur dans `config.toml`.

### A.3 Implémentation du module `llm_client`

```python
# invest_watch/llm_client.py
"""
Interface LLM — port swappable.
Implémentation : IBM watsonx.ai via ibm-watsonx-ai SDK.
Remplaçable par un stub de test ou le mode 'off' sans modifier
le reste du code.
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class LLMDegradedError(Exception):
    """Levée quand le circuit-breaker est ouvert ou le mode off activé."""


@dataclass
class LLMClientConfig:
    enabled: bool = True
    model_id: str = "ibm/granite-3-8b-instruct"   # surchargé par .env
    url: str = "https://eu-de.ml.cloud.ibm.com"    # surchargé par .env
    project_id: str = ""                            # surchargé par .env
    api_key: str = ""                               # surchargé par .env
    max_new_tokens: int = 700
    temperature: float = 0.1
    top_p: float = 0.9
    timeout_seconds: int = 120
    max_retries: int = 2
    circuit_breaker_threshold: int = 3


class WatsonxLLMClient:
    """Implémentation concrète via ibm-watsonx-ai SDK (ModelInference)."""

    def __init__(self, config: LLMClientConfig) -> None:
        self._cfg = config
        self._consecutive_failures = 0
        self._degraded = False
        self._model = None  # initialisé à la demande (lazy)

        # Cache mémoire par run : {content_hash: str} -> résultat JSON str
        # Réinitialisé à chaque instanciation (= chaque run)
        self._cache: dict[str, str] = {}

    # ── initialisation paresseuse ──────────────────────────────────────────

    def _get_model(self):
        """Crée ModelInference si nécessaire (lazy, évite l'import au démarrage)."""
        if self._model is not None:
            return self._model

        from ibm_watsonx_ai import Credentials
        from ibm_watsonx_ai.foundation_models import ModelInference
        from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams

        credentials = Credentials(
            url=self._cfg.url,
            api_key=self._cfg.api_key,
        )
        params = {
            GenParams.MAX_NEW_TOKENS: self._cfg.max_new_tokens,
            GenParams.TEMPERATURE: self._cfg.temperature,
            GenParams.TOP_P: self._cfg.top_p,
        }
        self._model = ModelInference(
            model_id=self._cfg.model_id,
            credentials=credentials,
            params=params,
            project_id=self._cfg.project_id,
        )
        log.info("LLM initialisé : modèle=%s url=%s", self._cfg.model_id, self._cfg.url)
        return self._model

    # ── appel principal ────────────────────────────────────────────────────

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        """
        Envoie le prompt et retourne le texte généré (str brut).
        Gère le cache mémoire intra-run, les retries et le circuit-breaker.
        Lève LLMDegradedError si circuit ouvert ou mode off.
        """
        if not self._cfg.enabled:
            raise LLMDegradedError("LLM désactivé (llm.enabled = false)")

        if self._degraded:
            raise LLMDegradedError("Circuit-breaker ouvert — mode dégradé actif")

        # Cache mémoire intra-run
        if content_hash and content_hash in self._cache:
            log.debug("Cache hit LLM pour hash %s", content_hash)
            return self._cache[content_hash]

        last_exc: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                model = self._get_model()
                response = model.generate(prompt=prompt)
                result: str = response["results"][0]["generated_text"]
                self._consecutive_failures = 0
                if content_hash:
                    self._cache[content_hash] = result
                return result

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._consecutive_failures += 1
                log.warning(
                    "Erreur LLM (tentative %d/%d) : %s",
                    attempt + 1, self._cfg.max_retries + 1, exc,
                )

                # Pas de retry sur 4xx
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status and 400 <= int(status) < 500:
                    break

                # Circuit-breaker
                if self._consecutive_failures >= self._cfg.circuit_breaker_threshold:
                    self._degraded = True
                    log.error(
                        "Circuit-breaker LLM ouvert après %d échecs consécutifs",
                        self._consecutive_failures,
                    )
                    raise LLMDegradedError("Circuit-breaker ouvert") from exc

                # Backoff exponentiel : 2 s, 8 s, 30 s
                if attempt < self._cfg.max_retries:
                    backoff = (2 ** (attempt + 1)) * 2
                    log.info("Backoff %d s avant la prochaine tentative", backoff)
                    time.sleep(backoff)

        raise LLMDegradedError(f"LLM indisponible après {self._cfg.max_retries + 1} tentatives") from last_exc

    # ── utilitaires ────────────────────────────────────────────────────────

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def reset_circuit_breaker(self) -> None:
        """Réinitialisé au run suivant par l'orchestrateur."""
        self._degraded = False
        self._consecutive_failures = 0


def build_llm_client(config: LLMClientConfig) -> WatsonxLLMClient:
    """Factory — remplacer par un stub dans les tests."""
    return WatsonxLLMClient(config)
```

### A.4 Chargement de la config dans `config_manager`

```python
# Dans config_manager.py — section LLM
import os

def _load_llm_config(raw: dict) -> LLMClientConfig:
    llm_section = raw.get("llm", {})
    return LLMClientConfig(
        enabled=llm_section.get("enabled", True),
        # Modèle et région : priorité .env > config.toml
        model_id=os.environ.get("WATSONX_MODEL_ID")
                 or llm_section.get("model", "ibm/granite-3-8b-instruct"),
        url=os.environ.get("WATSONX_URL")
            or llm_section.get("endpoint", "https://eu-de.ml.cloud.ibm.com"),
        project_id=os.environ.get("WATSONX_PROJECT_ID")
                   or llm_section.get("project_id", ""),
        api_key=os.environ.get("WATSONX_API_KEY", ""),
        max_new_tokens=llm_section.get("max_new_tokens", 700),
        temperature=llm_section.get("temperature", 0.1),
        top_p=llm_section.get("top_p", 0.9),
        timeout_seconds=llm_section.get("timeout_seconds", 120),
        max_retries=llm_section.get("max_retries", 2),
        circuit_breaker_threshold=llm_section.get("circuit_breaker_threshold", 3),
    )
```

**Priorité de résolution** : variable `WATSONX_MODEL_ID` dans `.env` ou l'environnement > valeur dans `config.toml` > valeur par défaut codée. Ainsi, changer de modèle ou de région ne nécessite **aucune modification du code** — uniquement une mise à jour du `.env`.

### A.5 Stub de test

```python
# tests/stubs/llm_stub.py
from invest.llm_client import LLMDegradedError

class LLMStub:
    """Remplace WatsonxLLMClient dans tous les tests. Zéro appel réseau."""

    def __init__(self, response: str = '{"faits": [], "importance": "faible"}',
                 raise_after: int = -1):
        self._response = response
        self._raise_after = raise_after
        self._call_count = 0
        self.is_degraded = False

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        self._call_count += 1
        if self._raise_after >= 0 and self._call_count > self._raise_after:
            raise LLMDegradedError("Stub: circuit simulé ouvert")
        return self._response

    def reset_circuit_breaker(self) -> None:
        self.is_degraded = False
```

### A.6 Régions disponibles (référence)

| Code région | URL endpoint | Remarque |
|---|---|---|
| `us-south` | `https://us-south.ml.cloud.ibm.com` | Dallas — région par défaut IBM Cloud |
| `eu-de` | `https://eu-de.ml.cloud.ibm.com` | Frankfurt — **recommandé RGPD** |
| `eu-gb` | `https://eu-gb.ml.cloud.ibm.com` | Londres |
| `jp-tok` | `https://jp-tok.ml.cloud.ibm.com` | Tokyo |
| `au-syd` | `https://au-syd.ml.cloud.ibm.com` | Sydney (SDK ≥ 1.1.23) |

> Pour lister les modèles disponibles dans votre région :
> ```bash
> TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
>   -H "Content-Type: application/x-www-form-urlencoded" \
>   -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=$WATSONX_API_KEY" \
>   | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
>
> curl -s -H "Authorization: Bearer $TOKEN" \
>   "https://eu-de.ml.cloud.ibm.com/ml/v1/foundation_model_specs?version=2024-09-16" \
>   | python -m json.tool | grep '"model_id"'
> ```

### A.7 Section `[llm]` dans `config.toml` (valeurs non-secrètes)

```toml
[llm]
enabled = true
# model et endpoint sont surchargés par WATSONX_MODEL_ID et WATSONX_URL dans .env
# Les valeurs ci-dessous servent de fallback si les variables d'env sont absentes.
model     = "ibm/granite-3-8b-instruct"
endpoint  = "https://eu-de.ml.cloud.ibm.com"
# project_id est surchargé par WATSONX_PROJECT_ID dans .env
project_id = ""

max_new_tokens            = 700
temperature               = 0.1
top_p                     = 0.9
timeout_seconds           = 120
max_retries               = 2
circuit_breaker_threshold = 3
# max_calls_per_run : informatif uniquement (budget illimité — DA-02)
max_calls_per_run         = 0
```

> **Note DA-02** : `max_calls_per_run = 0` signifie « pas de limite ». La valeur est conservée à titre informatif dans `daily_run.stats_json` pour le suivi de l'usage, mais n'est jamais bloquante.
