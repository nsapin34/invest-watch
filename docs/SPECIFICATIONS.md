# Spécifications fonctionnelles et techniques — Application « invest-watch »

Suivi quotidien automatisé des informations publiées la veille concernant les compagnies détenues par un utilisateur (portefeuille mondial, forte composante Chine), à partir de sources Internet gratuites, stockage local et analyse optionnelle via un LLM sur IBM watsonx.ai.

> **Version 1.3 — 2026-09-08 — Options §4.6, §4.7 et §17 arbitrées ; interface graphique V2 et idées orientées investissement (A1, A2, A3, B1, B2, B5, pack Chine) intégrées à la feuille de route V2 (§17). Décision D19 (forme de l'interface) à confirmer. Toutes les autres options par défaut sont validées telles quelles.**
> **Principe directeur :** tout service optionnel indisponible (LLM, source, moteur, réseau) déclenche le mode dégradé ; l'application ne s'arrête jamais par absence d'un service optionnel.

---

## 1. Résumé exécutif

**Le problème.** Un investisseur personnel détient des actions de compagnies réparties sur tous les continents, dont une part significative en Chine. Chaque matin, il doit savoir ce qui s'est passé *la veille* sur ces compagnies et leurs marchés : résultats financiers, réglementation, technologie, concurrence, géopolitique. Sans outil, cela demande des heures de lecture de presse fragmentée, multilingue, redondante et de qualité inégale.

**La solution (MVP).** Une application Python locale, sans interface graphique, lancée chaque matin par le planificateur de tâches du système d'exploitation. Elle interroge des sources gratuites (flux RSS en priorité, moteurs de recherche gratuits en secours), filtre les contenus publiés la veille, extrait et nettoie le texte des pages, détecte langue et date, classe et note les informations par compagnie / marché / catégorie, détecte doublons et reprises, résume (watsonx.ai si disponible, sinon par heuristiques), stocke tout dans un fichier SQLite et produit un rapport quotidien Markdown + JSON organisé par compagnie et par marché. Chaque information reste traçable jusqu'à sa source via un lien court local.

**Principes directeurs :**

- **Gratuité et conformité :** sources gratuites, respect de `robots.txt`, des conditions d'utilisation et des débits.
- **Traçabilité :** aucun fait sans référence à un article conservé et à une URL originale.
- **Le LLM n'invente rien :** résumés fondés uniquement sur les contenus collectés, sortie JSON validée, références obligatoires.
- **Échec partiel tolérable :** une source en panne (ou le LLM) n'interrompt pas le traitement des autres sources.
- **Simplicité :** une CLI, un fichier de base, zéro serveur, zéro interface graphique.

## 2. Hypothèses et questions ouvertes

Chaque question non arbitrée reçoit une **valeur par défaut raisonnable**, à confirmer avant développement. Les choix marqués « à trancher » doivent être arbitrés en §20.

| # | Question | Valeur par défaut proposée | Justification |
|---|---|---|---|
| Q1 | Format de la liste des compagnies | Fichier TOML + table SQLite, éditables via CLI `invest-watch company add/list/edit/remove` | Config unique ; la CLI évite les erreurs d'édition manuelle |
| Q2 | Identification des compagnies | Nom + ticker (opt.) + ISIN (opt.) + pays + site officiel + marché principal + alias (opt.) ; minimum : nom + pays | Les alias compensent les variantes (« BYD », « BYD Company Limited », « 比亚迪 ») ; l'ISIN sert de pivot de déduplication |
| Q3 | Définition des marchés et secteurs | Taxonomie à 3 niveaux : **marché** (zone/place : « Chine A », « Nasdaq »), **secteur** (niveau GICS), **industrie** (niveau GICS) ; hiérarchie par `parent_id`, éditable | Standard reconnu et stable ; zones personnalisables |
| Q4 | Langues des sources | Français, anglais, chinois (simplifié) ; personnalisable par compagnie | Couvre la presse mondiale + la Chine |
| Q5 | Fuseau de la « journée » | `Europe/Paris` (configurable) | La « veille » est définie dans ce fuseau ; compromis asiatique traité en §6.3 et §10 |
| Q6 | Heure de lancement | 07h00 locale | Après la fermeture des marchés asiatiques, avant l'ouverture européenne |
| Q7 | Durée max d'une collecte | 90 min global ; 30 s/page ; 10 s/flux | L'échec partiel est toléré (§10) |
| Q8 | Nombre max de compagnies suivies | 50 | Borne les volumes réseau et le coût LLM |
| Q9 | Type de base de données | SQLite — fichier unique `data/invest.db` | Simple, fiable, zéro service, FTS5 intégré |
| Q10 | Fournisseur de recherche gratuit | Cascade : RSS éditoriaux → Google News RSS → DuckDuckGo ; option Brave/Tavily | Voir §4 et §6 |
| Q11 | Limites et quotas des services gratuits | DDG ~1 req/3 s ; Brave ~2 000 req/mois (à vérifier à l'implémentation) ; Tavily ~1 000 crédits/mois ; **quota applicatif : 40 requêtes/jour** | Éviter blocage IP et dépassements |
| Q12 | Modèle watsonx.ai | Granite instruct (`ibm/granite-3-8b-instruct` — à confirmer au catalogue) | Interchangeable via l'interface `LLMClient` |
| Q13 | Authentification watsonx.ai | Clé API IAM + endpoint régional + `project_id` ; SDK officiel `ibm-watsonx-ai` | Clé via variable d'environnement / fichier `.env` restreint (§8.7) — choix utilisateur |
| Q14 | Format des résumés | Article : 3-6 phrases + JSON structuré ; digests : Markdown par sections avec références `#id` | JSON pour la machine, MD pour l'humain |
| Q15 | Conservation des données | Contenus textuels : 90 jours (purge auto) ; métadonnées : illimité ; backup quotidien | Respect du droit d'auteur + volume maîtrisé |
| Q16 | Rapport / notification quotidien | Non en V1 (rapport fichier + consultation CLI) | V2 : e-mail / Telegram |
| Q17 | Données envoyées au LLM | Titre, date, source, texte tronqué ≤ 8 000 caractères, liste compagnies/marchés candidats | Minimum nécessaire (§8.2) |
| Q18 | Erreurs | Logs + table `processing_error` + section du rapport + code de sortie non nul | Vérifiable par le planificateur |
| Q19 | Langue du rapport | Français | Articles cités en langue d'origine |
| Q20 | Datetimes | Stockage UTC ; seuils convertis dans le fuseau configuré | Évite les erreurs de décalage |

> **Décisions utilisateur prioritaires : D1 (fuseau), D6/D7 (moteurs), D8/D9 (watsonx.ai), D15 (taxonomie), D16 (envoi au LLM).**


## 3. Spécifications fonctionnelles

### 3.1 Problème traité, utilisateurs, fonctionnement quotidien

- **Problème** : une personne physique suit un portefeuille mondial (forte composante Chine) et perd du temps à chercher, lire, dédupliquer et classer manuellement les informations publiées la veille sur ses compagnies et leurs marchés.
- **Utilisateurs** : un investisseur individuel non professionnel, francophone, travaillant sous Windows (environnement cible), avec zéro interface graphique en V1.
- **Fonctionnement quotidien attendu** : chaque matin à 07h00, le planificateur lance `invest-watch run` ; l'application collecte, traite, stocke et produit `reports/YYYY-MM-DD.md` et `reports/YYYY-MM-DD.json` ; l'utilisateur consulte les résultats via `invest-watch report`, `invest-watch company X`, `invest-watch market Y`.

### 3.2 Limites du périmètre initial

1. Usage strictement personnel, mono-utilisateur, mono-poste.
2. Pas d'informations temps réel : les cours et les prix ne sont ni collectés ni calculés.
3. Collecte limitée à la journée précédente (J-1) ; pas de rattrapage historique de longue période.
4. Exports statiques (fichiers) ; pas d'API REST ni de serveur web.
5. Couverture : échantillon raisonnable des sources configurées, pas une indexation exhaustive du web.

### 3.3 Fonctionnalités explicitement exclues de la V1

- Interface graphique et application mobile.
- Notifications (push, e-mail, messageries).
- Gestion de portefeuille, valorisation, calcul de performance.
- Alertes de prix ou de seuils.
- Multi-utilisateurs, synchronisation cloud, interface web.
- Collecte temps réel / streaming.
- Traduction automatisée des articles.
- Raccourcissement de liens via un service externe (V1 = liens courts locaux, §4.5).
- Récupération de contenus derrière paywall.

### 3.4 Catalogue des fonctions (V1)

| # | Fonction | Description |
|---|---|---|
| F1 | Gestion des compagnies suivies | CRUD CLI, champs d'identification, activation, langues, alias |
| F2 | Gestion des marchés / secteurs / industries | CRUD CLI, hiérarchie, jeu par défaut GICS + zones |
| F3 | Association compagnie ↔ marchés | Table `company_market` ; relation n-n, poids optionnel |
| F4 | Recherche Internet | Cascade RSS → moteurs ; limite de débit ; quotas internes |
| F5 | Filtrage par date de publication | J-1 dans le fuseau configuré, tolérance ±6 h, marqueur `date_certaine` |
| F6 | Extraction du contenu des pages | HTTP poli (User-Agent identifié, robots.txt) ; HTML → texte |
| F7 | Métadonnées article | titre, date, source, auteur, URL originale + finale après redirection |
| F8 | Détection de la langue | `lingua` sur les 300 premiers caractères |
| F9 | Classification | compagnies / marchés / catégories (règles + enrichissement LLM optionnel) |
| F10 | Évaluation pertinence / importance | scores 0..1 + niveau (critique / haute / moyenne / faible) |
| F11 | Détection des doublons | 4 couches + regroupement d'événements (§7) |
| F12 | Résumé individuel des articles | LLM (JSON structuré) ou mode dégradé (extrait) |
| F13 | Résumé quotidien par compagnie | digeste Markdown citant les `#id` d'articles |
| F14 | Résumé quotidien par marché | idem |
| F15 | Résumé global | synthèse des points critiques + points à vérifier |
| F16 | Stockage des données | insertions / upsert transactionnels SQLite |
| F17 | Génération de liens courts | code local 6-8 caractères unique ; rendu `[code](url)` |
| F18 | Journalisation | logs horodatés, rotation, niveaux |
| F19 | Relance après erreur | `processing_error` + reprise automatique / manuelle |
| F20 | Exécution automatique chaque matin | entrée CLI invocable par planificateur, verrou anti-double-exécution |
| F21 | Consultation / export | CLI + rapports Markdown / JSON |
| F22 | Calendrier des catalyseurs | dates de résultats, publications de ventes mensuelles, salons, échéances réglementaires, expirations d'accords ; option « à surveiller demain » |
| F23 | Détection de pics de mentions (momentum) | volume d'articles par compagnie sur 24 h vs ligne de base 30 j → alerte « il se passe quelque chose » (heuristique pure, zéro LLM) |
| F24 | Vérification croisée des chiffres entre sources | pour chaque cluster d'événements, le LLM extrait les chiffres clés de chaque source et signale les écarts entre elles (+1 appel LLM par cluster) |
| F25 | Thèse d'investissement par compagnie | champ local « ma thèse » (scénario haussier/baissier, seuils de sortie) + marquage automatique « conforte / contredit / neutre » sur chaque article pertinent |
| F26 | Tableau de bord des drivers macro/sectoriels | prix matières premières (lithium, cuivre, terres rares, acier), ventes VE Chine (CAAM), PMI Chine, décisions PBoC, taux et décisions de banques centrales (autres régions) |
| F27 | Sentiment & momentum 30-90 j | courbe de ton LLM + volume par compagnie / marché sur fenêtre glissante (greffé sur l'interface graphique V2) |


## 4. Choix technologiques

Principe : bibliothèques matures, gratuites, avec le minimum de dépendances externes. Chaque choix évalue rôle, avantages, limites, coûts / quotas, risque de dépendance et alternative.

### 4.1 Base de données locale

| Option | Rôle | Avantages | Limites | Risque | Alternative |
|---|---|---|---|---|---|
| **SQLite** (`sqlite3` stdlib) ✔ | Persistance dans un fichier unique | Zéro service, ACID, fiable, portable, FTS5 intégré, gratuit, aucun quota | Un seul écrivain à la fois (suffisant ici) ; backup à chaud via `VACUUM INTO` | Aucun | DuckDB (orientation analytique), PostgreSQL (surdimensionné en local) |

> **Recommandation : SQLite**, fichier `data/invest.db`, mode WAL activé, migrations versionnées en SQL gérées par le module `db_manager`.

### 4.2 Sources et bibliothèques de recherche Internet gratuites

| Option | Rôle | Avantages | Limites / quotas / coûts | Risque de dépendance | Alternative |
|---|---|---|---|---|---|
| **Flux RSS** (`feedparser`) ✔ | Découverte principale | Légal (associé aux conditions des éditeurs), metadata (titre, date, lien) fournies, coût nul | Couverture variable ; certains flux cessent d'exister | Moyen (flux qui s'éteignent) | Niveau 2 de recherche |
| **Google News RSS** | Recherche par mots-clés sans clé | Gratuit, multilingue (`hl=fr`, `hl=en`, `hl=zh-CN`), filtre « after/before » approximatif | Non documenté comme API ; disponibilité non garantie ; débit à limiter | Élevé | DuckDuckGo, Brave |
| **DuckDuckGo** (`duckduckgo-search`) | Secours web sans clé | Gratuit, simple | Scraping de pages HTML : fragile, risque de blocage IP, conditions d'utilisation floues | Élevé | Brave / Tavily |
| **Brave Search API** (niveau gratuit) | Secours stable | ~2 000 requêtes/mois gratuites (à revérifier à l'implémentation), vraie API | Quota mensuel ; pérennité du niveau gratuit non garantie | Moyen | Tavily, Serper (payant) |
| **Tavily Search** (niveau gratuit) | Recherche orientée LLM | ~1 000 crédits/mois, paramètre de bornes de dates | Quota faible | Moyen | Brave |
| **APIs institutionnelles gratuites** (SEC EDGAR/FTS, HKEXnews, banques centrales, INSEE…) | Sources réglementaires et macro | Fiables, légales, structurées ; EDGAR impose ~1 req/s de politesse | Périmètre limité | Faible | — |
| **Flux institutionnels chinois gratuits** | Données macro et réglementaires Chine | Fiables, officiels, gratuits, structurés ; couvrent statistiques nationales, politique monétaire, marchés boursiers, ventes automobiles et presse financière de référence | Certains flux en chinois uniquement ; débit à respecter | Faible | — |

> **Recommandation : stratégie en cascade RSS éditoriaux → Google News RSS → DuckDuckGo**, avec quotas internes (cf. §6.6).

> **Flux institutionnels chinois** (détail complet en §21.B2) : NBS — Bureau national des statistiques (conférences de presse Jiang Jun + indicateurs IPC/PMI) ; PBoC — Banque populaire de Chine (communiqués de politique monétaire) ; CSRC — Commission de réglementation des valeurs mobilières de Chine ; SHSE/SZSE — Bourses de Shanghai et Shenzhen (divulgations des sociétés cotées) ; HKEXnews — Dépôt réglementaire de la Bourse de Hong Kong ; CAAM — Association des constructeurs automobiles de Chine (ventes mensuelles VE) ; presse financière : Caixin Global (RSS), South China Morning Post (RSS).

### 4.3 HTTP et extraction de contenu

| Option | Rôle | Avantages | Limites | Recommandation |
|---|---|---|---|---|
| `httpx` | Client HTTP | API moderne sync/async, timeouts, redirections, HTTP/2 optionnel | — | Utilisé partout, `follow_redirects=True` pour capturer l'URL finale |
| `trafilatura` | HTML → texte principal + date | Excellence du rendu, lit `datePublished` / `article:published_time`, multilingue | Dépendance de poids moyen ; à borner en taille de réponse | Extraction principale |
| `beautifulsoup4` + `lxml` | Parsing complémentaire (flux, meta, JSON-LD) | Souple | Plus verbeux | Ponctuel |
| `dateparser` | Dates multilingues / relatives | « 2 hours ago », formats chinois, etc. | Ambiguïtés de fuseau | Toujours paramétré avec le fuseau courant |
| `lingua-language-detector` | Détection de langue | Précision, aucun téléchargement | Plus lent | Sur 300 caractères ; repli `langdetect` |
| `feedparser` | Parsing RSS / Atom | Standard de fait | Certains flux non conformes | — |

### 4.4 Résumés par LLM

| Option | Rôle | Avantages | Limites / coûts | Recommandation |
|---|---|---|---|---|
| SDK `ibm-watsonx-ai` | Appels au modèle hébergé | Officiel IBM, gestion IAM (token), streaming, paramètres de génération | Pas de niveau gratuit public garanti (crédits de démonstration à vérifier sur le compte) ; paramètres à aligner sur la documentation du jour | Implémentation concrète derrière l'interface `LLMClient` |
| **Mode dégradé sans LLM** | Résumés par extraction | Gratuit, hors ligne, toujours fonctionnel | Résumés moins fins (extrait + faits heuristiques) | Toujours activable (`llm.enabled = false`) |

### 4.5 Création de liens courts

| Option | Avantages | Limites | Recommandation |
|---|---|---|---|
| **Code court local** (table `short_link`, 6-8 caractères aléatoires sécurisés) ✔ | Gratuit, hors ligne, l'URL n'est jamais envoyée à un tiers, traçabilité totale | Résolution uniquement via la base locale (suffisant pour les rapports locaux) | V1 |
| is.gd / TinyURL (APIs gratuites sans clé) | Vrais liens web publics | Dépendance externe, pannes, quotas, URL soumise à un tiers | Option V2, désactivée par défaut |

### 4.6 Planification et tâche de fond

| Option | Avantages | Limites | Recommandation |
|---|---|---|---|
| **Planificateur Windows (Task Scheduler)** ✔ **choix retenu** | Natif, démarrage au boot ou à la connexion, condition réseau, historique | Syntaxe CLI ancienne (`schtasks`) | V1 sur Windows — voir §12.1 |
| cron (Linux / macOS) | Standard | Diffère selon l'OS | Fournir les crontabs en documentation |
| Bibliothèque `schedule` (Python) | Simple d'écriture | Nécessite un processus permanent (fragile, fuites possibles) | Non recommandé |
| Conteneur Docker | Isolation | Surcharge, planification à reboucher | Non pour une V1 locale |


### 4.7 Stockage des secrets

| Option | Avantages | Limites | Recommandation |
|---|---|---|---|
| **Variables d'environnement / fichier `.env` restreint** ✔ **choix retenu** | Simple, universel, utilisable par le planificateur de tâches sans session graphique, compatible multi-OS | Fichier en clair sur disque — compensé par permissions 700, hors git, jamais dans logs/base/rapports | **V1** — cf. §11 et §8.7 |
| Trousseau OS via `keyring` (Windows Credential Manager, macOS Keychain, Linux Secret Service) | Clés jamais sur disque en clair, permissionnées par l'OS | Certains fournisseurs exigent une session graphique (gêne pour un run lancé sans session utilisateur) | Alternative / évolution optionnelle |
| Chiffrement Fernet de la config complète | Chiffré au repos | Gestion de la clé de chiffrement (serpent qui se mord la queue) | Évolution V2 |

### 4.8 Export des rapports

| Format | Usage | Décision |
|---|---|---|
| Markdown | rapport quotidien lisible par l'humain | V1 |
| JSON | export machine structuré (tous les champs) | V1 |
| **Interface web locale** | consultation des nouvelles, résumés et marchés + édition des compagnies/marchés suivis dans le navigateur (V2, §17) | **V2 — décision utilisateur** |
| HTML | export autonome d'une page de rapport | V2 |
| PDF | archivage imprimable | V3 — non indispensable |

## 5. Architecture proposée

### 5.1 Vue d'ensemble

```text
Planificateur OS (07h00) ───▶  invest-watch (CLI)
         │                    argparse : run, report, company, market,
         │                    status, errors, export, sources, migrate
         ▼
   ┌───────────────────────────────────────────────────────────┐
   │                      ORCHESTRATEUR                         │
   │   enchaîne les étapes du workflow (§10), tolère les        │
   │   échecs, met à jour daily_run, assemble le rapport        │
   └──┬──────┬──────┬────────┬────────┬────────┬───────┬────────┘
      │      │      │        │        │        │       │
 SEARCH   FETCHER CLEANER CLASSIFIER  DEDUP SUMMARIZER SHORT_LINK
 (rss/    (http+  (trafila- (règles+  (minhash) (LLM ou   (codes
 search/   robots) tura)     LLM opt.)            dégradé)  courts)
  api)
      │      │      │        │        │        │       │
      └──────┴──────┴────────┴────────┴────────┴───────┴────────┘
                                 │
                       ┌─────────┴──────────┐
                       │      DB_MANAGER    │  SQLite data/invest.db
                       │  schéma, migrations│  WAL + FTS5
                       │  repositories      │
                       └────────────────────┘
   CONF (TOML) · LOGGER · ERROR_MANAGER (retries/circuit-breaker) · METRICS
```

### 5.2 Modules et responsabilités

| Module | Responsabilité | Interactions principales |
|---|---|---|
| `config_manager` | Charger / valider `conf/config.toml`, résoudre `${VAR}` et les secrets (bases : environnement / fichier `.env` restreint ; trousseau OS en alternative), fournir une config immuable | Tous les modules à l'initialisation |
| `company_repository` | CRUD compagnies, alias, vecteurs de recherche (nom, ticker, ISIN, site) | orchestrateur, classificateur |
| `market_repository` | CRUD marchés / secteurs / industries, hiérarchie par parent | orchestrateur, classificateur |
| `search_engine` | Génération des requêtes, cascade RSS → Google News RSS → moteurs, limite de débit, résultats métadonnés (titre, url, date, source) | fetcher, sources |
| `fetcher` | Récupération HTTP polie des pages : User-Agent d'application, robots.txt (cache par hôte), timeouts, capture de l'URL finale, bornes de taille | cleaner, short_link |
| `cleaner` | trafilatura → texte principal ; suppression boilerplate ; extrait (400 caractères) ; normalisation | date_detector, lang_detector |
| `date_detector` | Date de publication : JSON-LD, meta tags, RSS, puis `dateparser` ; résultat « certaine / incertaine » | classificateur |
| `classifier` | Langue, catégories, compagnies / marchés associés (règles + enrichissement LLM optionnel), scores initiaux | db_manager, llm_client |
| `dedup` | Normalisation d'URL, hash SHA-256, similarité de titres (Jaccard), MinHash (`datasketch`), regroupement d'événements | db_manager |
| `llm_client` | Interface d'appel watsonx.ai : auth, prompts, parsing JSON, timeouts, retries, compteur d'appels, circuit-breaker | summarizer, classificateur |
| `summarizer` | Résumés d'article (JSON structuré), digests compagnie / marché / global ; mode dégradé | llm_client, db_manager |
| `short_link` | Génération du code court (aléatoire sécurisé, unicité en base), rendu markdown | db_manager |
| `db_manager` | Connexion SQLite, WAL, migrations versionnées, repositories, FTS5 | tous |
| `orchestrator` | Enchaîne le workflow quotidien, met à jour `daily_run`, assemble le rapport, gère reprise / verrou | tous |
| `logger` | logging stdlib, fichier rotatif + fichier par run, niveaux configurables | tous |
| `error_manager` | Enregistre `processing_error`, backoff, isolation par source, circuit-breaker LLM | orchestrateur, modules |
| `cli` | Commandes utilisateur (§13) | orchestrateur, repositories |
| `web_ui` **(V2)** | Interface web locale interactive : serveur lié à `127.0.0.1`, consultation des nouvelles / résumés / marchés du jour et des jours précédents, édition des compagnies et des marchés suivis, déclenchement d'un run ; agit uniquement via les repositories et l'orchestrateur | cli, orchestrateur, repositories |
| `insight_engine` **(V2)** | Idées orientées investissement (A1–B5, pack Chine, §17) : calendrier des catalyseurs, pics de mentions, fact-check chiffres, thèse d'investissement, drivers macro/secto, sentiment 30–90 j ; s'exécute en option après le workflow V1 | orchestrateur, classificateur, db_manager |

### 5.3 Règles d'interaction

1. **Aucun module n'accède directement à l'écriture en base** : tout passe par `db_manager` (repositories) ; chaque insertion est rattachée à un `daily_run`.
2. **Tous les appels réseau passent par `fetcher` / `search_engine`** : limiter de débit global (jetons par seconde), User-Agent unique, timeouts.
3. **`LLMClient` est un port** : implémentation watsonx.ai, stub de test, et mode « off » (dégradé) sans la moindre dépendance.
4. **Chaque étape du workflow est idempotente** (upsert sous contraintes d'unicité) — la reprise ne duplique rien.


## 6. Stratégie de recherche Internet

Objectif : couvrir la veille sur les compagnies suivies et leurs marchés avec des sources gratuites, légales et configurables.

### 6.1 Génération des requêtes

- **Par compagnie** (à partir des alias et templates) :
  - `{nom}` · `{nom court}` · `{ticker}` (ex. `BYDDY`) · `{nom} + {mot-clé industrie}` ;
  - variante chinoise pour les compagnies chinoises (nom officiel, ex. `比亚迪`).
- **Par marché / secteur** : `{intitulé} news`, `{nom marché} regulation`, `{secteur} {pays}`, etc.
- **Volumes** : par défaut **3 requêtes / compagnie + 2 requêtes / marché actif**, plafond global **40 requêtes par run** (section `max_queries_per_run`).
- **Sécurité des requêtes** : les templates n'intègrent jamais de texte libre non sanitisé ; échappement des caractères réservés des moteurs.

### 6.2 Limitation aux informations de la journée précédente

1. **Flux RSS** : filtre sur `published` / `published_parsed` converti en UTC, comparé à l'intervalle [J-1 00h00, J-1 23h59:59 **dans le fuseau configuré**, converti en UTC].
2. **Moteurs** : rétention des résultats dont la date extraite est dans l'intervalle ; les filtres « after/before » de Google News RSS sont des **indications**, jamais une vérité.
3. **Date inconnue** : article conservé avec `date_certaine = false`, score minoré, signalé dans le rapport.
4. **Tolérance** : ±6 heures autour de l'intervalle (configurable) pour ne pas manquer une publication tardive ; **pas de collecte historique de rattrapage en V1**.

### 6.3 Fuseaux horaires

- Tout est stocké en UTC ; l'intervalle est calculé dans le fuseau de l'utilisateur (Q5/D1).
- Pour les compagnies asiatiques, une publication du matin chinois tombe la veille en Europe : compromis assumé, compensé par la tolérance ±6 h et par le ciblage des flux boursiers locaux.
- `dateparser` est toujours configuré avec le fuseau courant ; jamais de parsing naïf.

### 6.4 Priorisation des sources

| Rang | Type | Exemples V1 configurables |
|---|---|---|
| 1 | **Officiel compagnie** | Communiqués et pages relations investisseurs (page + flux RSS si existant) |
| 2 | **Réglementaire** | SEC EDGAR/FTS, HKEXnews, SHSE/SZSE, CSRC, autorités locales ; **flux institutionnels chinois** : NBS (IPC/PMI), PBoC, CAAM (ventes VE), Caixin RSS, SCMP RSS |
| 3 | **Flux éditorial de référence** | Google News RSS par langue ; flux RSS des médias financiers vérifiés |
| 4 | **Grands médias mondiaux** (accès public) | Requêtes DuckDuckGo / Brave ciblant des domaines de confiance |
| 5 | **Spécialisés** | Sites sectoriels (batteries, semi-conducteurs, matières premières, Chine…) |

La source officielle d'un événement (rang 1-2) est **conservée même si redondante** : c'est la référence pour vérification.


### 6.5 Filtrage de la qualité

- Domaines autorisés / listes noires (contenus générés, fermes à clics, agrégateurs sans valeur ajoutée) dans `[sources]` et table `source`.
- Contraintes minimales : titre non vide, texte extrait ≥ 200 caractères, date plausible, langue reconnue.
- Détection de boilerplate : ratio texte utile / balisage ; pages basées sur du contenu publicitaire → exclues.
- Articles sans lien vers les compagnies / marchés suivis → statut `unrelated`, non affichés par défaut.

### 6.6 Conformité, robots.txt et limites de débit

- **User-Agent identifié** : `invest-watch/1.0 (+adresse de contact)` — jamais un User-Agent de navigateur déguisé.
- **robots.txt** : respecté pour les pages web (`urllib.robotparser`, cache par hôte) ; les flux RSS ne sont pas soumis à robots.txt mais aux conditions d'utilisation des éditeurs.
- **Débit** : 1 requête / hôte / 2 s minimum par défaut (configurable), avec jitter aléatoire ; quota interne quotidien (40 requêtes moteur).
- **Réponse aux codes HTTP** : 429 → attendre `Retry-After` ; 5xx → backoff exponentiel (2 tentatives) ; 410/403 → source marquée en erreur (`processing_error`, `source.error_count`).
- **Zone grise documentée** : le scraping HTML de moteurs (DuckDuckGo) est le dernier recours et se désactive via `use_search_engines = false`.

### 6.7 Pages inaccessibles, paywalls, contenus dynamiques

- Timeout connect/read ; réponses > 2 Mo rejetées ou tronquées.
- **Paywalls** : on conserve uniquement les métadonnées (titre, date, source, extrait OpenGraph / meta description) — jamais de contournement.
- **Contenu dynamique** : V1 **sans navigateur headless** ; si le texte n'est pas servi côté serveur, seul le résumé métadonnées est conservé.

### 6.8 Information originale vs reprise

- La **source primaire** d'un événement est celle avec la date la plus ancienne **ou** le rang 1-2 (officiel).
- Les autres sont marquées `redundant` / `duplicate` (§7), sauf si leur contenu diffère de plus de 10 % (seuil `minhash_threshold`) → `complementary` et conservées comme sources additionnelles.

### 6.9 Mécanisme de configuration des sources

- Table `source` éditable par CLI (`invest-watch source add/list/disable/validate`) et/ou sections `[[sources]]` du TOML.
- Champs : URL, type (`rss` / `search` / `website` / `api`), langue, rang de priorité, débit spécifique, actif.
- `invest-watch sources validate` teste chaque source (code HTTP, parse RSS) et signale les flux morts — intégré aux métriques (§17).

## 7. Stratégie de déduplication et non-redondance

### 7.1 Couches successives (dans l'ordre d'exécution)

| # | Couche | Règle | Statut attribué |
|---|---|---|---|
| 1 | **URL normalisée** | Normalisation : schéma/hôte minuscules, suppression des paramètres de suivi (`utm_*`, `fbclid`, `gclid`, `mc_*`), suppression du fragment, URL finale après redirection | `duplicate` (doublon exact, référence à l'article initial) |
| 2 | **Hash du contenu** | SHA-256 du texte normalisé (minuscules, espaces uniformisés) | `duplicate` si hash identique |
| 3 | **Similarité des titres** | Jaccard sur tokens normalisés (stopwords retirés, stem strippé) ≥ 0.85 | `duplicate` (même dépêche) |
| 4 | **Similarité du contenu** | MinHash (`datasketch`, 128 permutations), cosinus ≥ 0.9 (paramétrable) | `redundant` (quasi-reprise) |
| 5 | **Regroupement d'événements** | Clustering : même compagnie + fenêtre ±24 h + similarité ≥ seuil | le premier devient « tête d'événement » (`event_id`), les suivants lui sont rattachés |

### 7.2 Conservations spécifiques

- **Plusieurs sources conservées** pour un même événement si : la source est officielle / institutionnelle (rang 1-2), **ou** l'article apporte des faits nouveaux (différence > 10 %), **ou** les dépêches rapportent des détails distincts.
- **Statuts possibles** : `new` (première observation), `updated` (faits nouveaux sur un événement déjà suivi), `redundant` (même dépêche, sans valeur ajoutée), `duplicate` (URL/hash identiques), `unrelated`.
- **Historique** : table `article_version` (traces des changements de hash/titre/extrait) ; `article.update_count` incrémenté.

### 7.3 Décision « événement nouveau vs mise à jour »

Un événement est **nouveau** si : aucun article des **14 derniers jours** n'appartient au même cluster **et** les faits clés (entités, chiffres, dates) diffèrent matériellement.
Il est **mis à jour** si un cluster existe **et** le contenu diffère ≥ seuil (3 faits supplémentaires distincts constatés).
Sinon → `redundant`.

### 7.4 Anti-redondance jour après jour

- À chaque run, les articles des **7 derniers jours** sont chargés en mémoire (hash, titres, MinHash) pour comparaison.
- Seuils dans `[dedup]`. Exception : les articles officiels (rang 1-2) d'un nouveau run sont toujours conservés même redondants, car leur date de mise à jour peut être significative.
- Le rapport quotidien **ne réaffiche pas** les `duplicate` / `redundant` des jours précédents ; il affiche les `updated` avec la mention « mise à jour depuis le DD/MM ».


## 8. Intégration du LLM IBM watsonx.ai

### 8.1 Types d'opérations confiées au LLM

1. **Résumé d'article** (JSON structuré : faits, impacts, incertitudes, catégorie, besoin de vérification, scores).
2. **Digeste quotidien par compagnie** et **par marché** (à partir des résumés d'articles).
3. **Résumé global** (à partir des digests).
4. (Optionnel, désactivable) **Reclassification** d'un article quand la confiance des règles est faible — révision bornée et validée de la catégorie et des scores.

Exclu : génération de contenu libre, décisions d'achat/vente, traduction (hors V2).

### 8.2 Données envoyées et non envoyées

**Envoyées** (uniquement si l'article passe le filtre de pertinence) :
- Titre, date de publication, nom de la source, langue détectée ;
- Texte extrait tronqué à **8 000 caractères** (configurable) — sinon, extrait de 1 500 caractères ;
- Liste des compagnies / marchés candidats déjà classés par règles.

**Jamais envoyées** : clés API, chemins locaux, configuration, identifiants internes (`daily_run`, erreurs), contenus hors budget, informations sans lien avec le suivi, données personnelles.

### 8.3 Prompts

**Principes communs** : rôle système factuel, sortie **JSON strict**, interdiction d'ajouter une information absente du texte (« si l'information n'est pas dans le texte, répondre `null` / [] »), préfixe de réponse `{`.

**Prompt — résumé d'article (trame à adapter à l'implémentation)** :

```text
Tu es un analyste factuel. Tu résumes UNIQUEMENT le contenu fourni.
CONTEXTE :
- Compagnies suivies : {noms}
- Marchés suivis : {noms}
TEXTE À ANALYSER (langue : {langue}) :
--- DÉBUT ---
{titre}
{date}
{texte}
--- FIN ---
Produis UNIQUEMENT du JSON valide selon ce schéma :
{
  "langue_resume": "fr",
  "titre_resume": "...",
  "categorie": "financial_results|corporate_actions|mergers_acquisitions|regulator|product|operations|macro|geopolitical|competitive|sustainability|other",
  "compagnies_concernees": ["..."],
  "marches_couverts": ["..."],
  "faits": ["..."],
  "consequences_potentielles": [
    {"cible": "...", "impact": "positive|negative|mixed|unknown",
     "horizon": "court|moyen|long|inconnu", "confidence": "haute|moyenne|basse"}
  ],
  "incertitudes_ou_conflits": ["..."],
  "verification_humaine_requise": true,
  "importance": "critique|haute|moyenne|faible",
  "pertinence": 0.0,
  "note_contradictions": ["..."]
}
Regles : ne rien inventer ; si un champ est inconnu, utiliser null ou [] ;
les faits doivent etre verification dans le texte fourni.
```

**Prompt — résumé quotidien par compagnie** : entrée = liste d'articles (titre, date, source, résumé structuré et son `#id`) ; sortie = digeste Markdown avec **chaque fait référencé** par le `#id` de l'article (`[a12]`), structure fixe : `## Compagnie`, `### Faits rapportés`, `### Conséquences potentielles`, `### Points à vérifier`. Mêmes exigences pour les digests marchés et le global (le global ajoute la section « Informations critiques »). Schémas identiques au niveau champ mais en Markdown, avec obligation de citer `[#id]` après chaque affirmation.

### 8.4 Format de réponse attendu et validation

- Sortie parsée en JSON ; **validation par schéma JSON strict** : énumérations, bornes des scores (0..1), longueur maximale.
- JSON invalide → 1 seule tentative corrective (« le JSON était invalide : corrige-le ») ; à défaut, repli dégradé + erreur consignée.
- Les scores LLM sont **bornés puis ré-étalonnés** par le classificateur (pertinence 0..1, importance sur 4 niveaux) — jamais pris tels quels.

### 8.5 Erreurs, délais d'attente, reprises

- Timeouts : connexion 10 s, total **120 s par appel** (configurable).
- Retries : 3 tentatives avec backoff exponentiel (2 s, 8 s, 30 s) pour 5xx / timeout / réseau ; **aucun retry** sur 4xx ou quotas dépassés.
- **Circuit-breaker** : 3 échecs consécutifs → `daily_run.llm_status = degraded` pour le reste du run ; le workflow bascule en mode dégradé ; le compteur se réinitialise au run suivant.
- Cache strict : pas de second appel pour un même `content_hash`.


### 8.6 Réduction des coûts et du nombre d'appels

- Appel LLM **uniquement** pour les articles pertinents (`relevance_score ≥ relevance_min`) et **un seul appel par article**.
- Troncature systématique : texte ≤ 8 000 caractères (au-delà : mode dégradé pour cet article, pas de résumé par morceaux en V1).
- **Cache par hash** : un contenu déjà résumé n'est jamais résumé de nouveau.
- **Borne d'appels par run** : `max_calls_per_run = 150` ; le compteur est exposé dans `daily_run.stats_json` et le rapport.
- Les digests réutilisent les résumés d'articles : aucun recalcul.

### 8.7 Authentification et gestion sécurisée

- **Clé API IAM via variable d'environnement ou fichier `.env` restreint** (`invest_watch/.env`, permissions 700, hors git) — résolue par le config-manager comme `${WATSONX_API_KEY}` ; le trousseau OS (`keyring`, item `invest-watch/watsonx-api-key`) reste une alternative documentée.
- Endpoint régional, `project_id` / `space_id`, modèle, paramètres de génération : dans `[watsonx]` du TOML (non secrets).
- Aucun secret dans les logs, les rapports ou les exports.

### 8.8 Traçabilité, inventaire et mode dégradé

- Chaque `summary` porte : `model_id`, `model_version`, `prompt_version`, `llm_used`, `validation_status`, `article_ids` (références `#id`).
- Les résumés citent leurs sources ; chaque article garde son URL originale + son code court.
- `invest-watch status` affiche la disponibilité du LLM ; chaque rapport débute par la ligne « Analyse LLM : watsonx.ai (modèle) — disponible / dégradée / désactivée ».
- **Mode dégradé complet** : sans SDK, sans clé, sans réseau LLM — les résumés sont produits par heuristiques (extrait de tête + premiers faits), marqués `llm_used = 0`. Le workflow ne plante jamais par absence de LLM.

## 9. Modèle de données

Base `data/invest.db` (SQLite, WAL). Conventions : tables en minuscules, clés `INTEGER PRIMARY KEY AUTOINCREMENT`, dates stockées en **UTC** (TEXT ISO-8601), index explicites, contraintes `CHECK` pour les énumérations. « Oblig. » = champ obligatoire.

### 9.1 Tables

**`company`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | auto-increment |
| name | TEXT | oui | UNIQUE (comparaison case-insensitive) |
| legal_name | TEXT | | |
| ticker | TEXT | | index |
| isin | TEXT | | UNIQUE si présent, index |
| country_code | TEXT | | index (ISO 3166-1 alpha-2) |
| currency | TEXT | | (ISO 4217) |
| website | TEXT | | |
| primary_market_id | INTEGER | | FK → market.id |
| summary | TEXT | | |
| active | INTEGER | oui | défaut 1 |
| created_at / updated_at | TEXT | oui | UTC ISO-8601 |

**`market`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | |
| name | TEXT | oui | UNIQUE |
| code | TEXT | | UNIQUE (ex. « CN.A », « GICS.2510 ») |
| type | TEXT | oui | CHECK in ('market','sector','industry') |
| parent_id | INTEGER | | FK → market.id (hiérarchie) |
| active | INTEGER | oui | |
| created_at / updated_at | TEXT | oui | |

**`company_market`** (relation n-n)

| Champ | Type | Oblig. | Contrainte |
|---|---|---|---|
| company_id | INTEGER | oui | FK → company.id, ON DELETE CASCADE |
| market_id | INTEGER | oui | FK → market.id, ON DELETE CASCADE |
| weight | REAL | | importance relative, défaut 1.0 |

PK composite (company_id, market_id).

**`source`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | |
| name | TEXT | oui | UNIQUE |
| type | TEXT | oui | CHECK in ('rss','search','website','api') |
| url | TEXT | oui | |
| lang | TEXT | | défaut '*', index |
| priority | INTEGER | oui | défaut 5, index |
| rate_limit_per_sec | REAL | | défaut 0.5 |
| active | INTEGER | oui | défaut 1 |
| last_success_at / last_error_at | TEXT | | |
| error_count | INTEGER | oui | défaut 0 |
| created_at / updated_at | TEXT | oui | |

**`search_alias`**

| Champ | Type | Oblig. | Contrainte |
|---|---|---|---|
| id | INTEGER PK | oui | |
| entity_type | TEXT | oui | CHECK in ('company','market') |
| company_id | INTEGER | | FK → company.id (si compagnie) |
| market_id | INTEGER | | FK → market.id (si marché) |
| term | TEXT | oui | |
| lang | TEXT | | défaut '*' |
| priority | INTEGER | | défaut 5 |
| active | INTEGER | oui | |
| created_at | TEXT | oui | |

Contrainte d'exclusivité : `CHECK ((entity_type='company' AND company_id IS NOT NULL AND market_id IS NULL) OR (entity_type='market' AND market_id IS NOT NULL AND company_id IS NULL))`.


**`article`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | auto-increment |
| run_id | INTEGER | oui | FK → daily_run.id, index |
| source_id | INTEGER | | FK → source.id, index |
| guid | TEXT | | UNIQUE si présent (identifiant du flux) |
| url_original | TEXT | oui | UNIQUE |
| url_normalized | TEXT | oui | UNIQUE, index |
| title | TEXT | oui | |
| author | TEXT | | |
| language | TEXT | | index |
| published_at | TEXT (UTC) | | index |
| date_certaine | INTEGER | oui | 0 = estimée |
| collected_at | TEXT (UTC) | oui | |
| content | TEXT | | texte conservé, purge 90 j |
| excerpt | TEXT | | 400 premiers caractères, conservé |
| content_hash | TEXT | | UNIQUE, index (SHA-256) |
| title_hash | TEXT | | index |
| short_code | TEXT | oui | UNIQUE |
| status | TEXT | oui | CHECK in ('new','updated','redundant','duplicate','unrelated') |
| event_id | INTEGER | | FK → article.id (tête de cluster), index |
| dedup_reason | TEXT | | |
| relevance_score | REAL | | défaut 0 |
| importance_level | TEXT | | CHECK in ('critique','haute','moyenne','faible') |
| requires_human_check | INTEGER | | défaut 0 |
| word_count | INTEGER | | |
| first_seen_run_id | INTEGER | | FK → daily_run.id |
| update_count | INTEGER | | défaut 0 |
| flagged | INTEGER | | défaut 0 (attention utilisateur) |
| created_at / updated_at | TEXT | oui | |

**`article_company`** — `article_id` FK oui, `company_id` FK oui, `mention_count` INTEGER, `relevance_in_article` REAL ; PK (article_id, company_id).
**`article_market`** — identique à `article_company` avec `market_id`.
**`article_category`** — `article_id` FK oui, `category_id` FK oui, `confidence` REAL ; PK (article_id, category_id).

**`category`** — `id` PK, `name` TEXT UNIQUE oui, `description`.

**`summary`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | |
| run_id | INTEGER | oui | FK → daily_run.id, index |
| type | TEXT | oui | CHECK in ('article','company','market','global') |
| ref_id | INTEGER | | article / company / market id visé |
| content | TEXT | oui | Markdown (digest) ou JSON (article) |
| json_payload | TEXT | | JSON structuré de l'article |
| model_id / model_version | TEXT | | traçabilité |
| prompt_version | TEXT | | |
| llm_used | INTEGER | oui | 0 ou 1 |
| content_hash | TEXT | | cache (article concerné) |
| validation_status | TEXT | oui | CHECK in ('ok','degraded','invalid') |
| article_ids | TEXT | | JSON liste des `#id` référencés |
| token_usage | INTEGER | | optionnel |
| created_at | TEXT | oui | |

Index (run_id, type, ref_id) ; contrainte `UNIQUE (run_id, type, ref_id)`.

**`daily_run`**

| Champ | Type | Oblig. | Contrainte / Index |
|---|---|---|---|
| id | INTEGER PK | oui | |
| run_date | TEXT | oui | UNIQUE (YYYY-MM-DD) |
| period_start / period_end | TEXT (UTC) | oui | |
| status | TEXT | oui | CHECK in ('pending','running','completed','partial','failed','cancelled') |
| started_at / finished_at | TEXT | | |
| stats_json | TEXT | | nombre de résultats, doublons, appels LLM, durées |
| error_count | INTEGER | | défaut 0 |
| llm_status | TEXT | | CHECK in ('ok','degraded','off') |
| trigger | TEXT | | CHECK in ('scheduler','manual','retry') |
| created_at | TEXT | oui | |

**`processing_error`**

| Champ | Type | Oblig. | Contrainte |
|---|---|---|---|
| id | INTEGER PK | oui | |
| run_id | INTEGER | | FK → daily_run.id |
| step | TEXT | oui | |
| source_id | INTEGER | | FK → source.id |
| article_id | INTEGER | | FK → article.id |
| error_type | TEXT | oui | |
| message | TEXT | | |
| traceback | TEXT | | |
| retry_count | INTEGER | | défaut 0 |
| status | TEXT | oui | CHECK in ('new','retrying','failed','resolved') |
| created_at / updated_at | TEXT | oui | |

**`short_link`** — `short_code` TEXT PK, `url_original` TEXT UNIQUE oui, `article_id` FK, `provider` TEXT défaut 'local', `external_url` TEXT, `created_at` oui.

**`article_version`** — `id` PK, `article_id` FK oui index, `content_hash`, `title`, `excerpt`, `changed_at` oui, `change_reason`.

**`app_meta`** — `key` TEXT PK, `value` TEXT (version de schéma, dernier run, cache catalogue LLM…).

### 9.2 Données exigées stockées

URL originale ✔ · lien court / identifiant ✔ (`short_code`) · titre ✔ · contenu textuel ✔ · extrait ✔ · source ✔ · date de publication ✔ · date de collecte ✔ · compagnies ✔ (junction) · marchés ✔ (junction) · catégories ✔ (junction) · score de pertinence ✔ · score d'importance ✔ · empreinte hash ✔ · résumé généré ✔ · version du modèle ✔ · références des articles du résumé ✔ (`summary.article_ids`).

### 9.3 Recherche plein texte et conservation

- Table virtuelle **FTS5** `article_fts` (title, excerpt, content) synchronisée aux insertions — recherche CLI `invest-watch search "mot"`.
- **Purge quotidienne** : `content` des articles de plus de 90 jours mis à `NULL` (+ FTS correspondant) ; les métadonnées, extraits, résumés et liens restent.


## 10. Workflow quotidien

**Préambule commun** : verrou `data/run.lock` (créé en mode exclusif, horodaté) — si présent et jeune (< 2 h), le second processus sort immédiatement (code 0) ; sinon possibilité de reprise forcée via `--force`. Insertion de `daily_run (run_date, status = 'running')`.

| Étape | Actions | Sortie | En cas d'échec |
|---|---|---|---|
| 1. Période | Intervalle J-1 dans le fuseau configuré, converti en UTC | `period_start`, `period_end` | arrêt du run |
| 2. Charger config | compagnies + marchés actifs, alias, sources actives | listes en mémoire | arrêt du run |
| 3. Générer requêtes | 3 / compagnie + 2 / marché, par langue, plafonnées | file de requêtes dédupliquées | continuer avec l'existant |
| 4. Interroger sources | cascade RSS → Google News RSS → moteurs, avec débit | candidats bruts (titre, url, date, source) | **jamais bloquant** : `processing_error`, statut `partial` |
| 5. Filtrer par date | intervalle ± tolérance ; affectation `date_certaine` | candidats retenus | idem |
| 6. Extraire contenus | fetcher + trafilatura | contenu, extrait, URL finale | page en échec → exclue ou `fetch_error` |
| 7. Nettoyer texte | boilerplate, normalisation | texte propre + hash | — |
| 8. Dédupliquer | couches §7 vs base (7 jours) + intra-run | statuts, `event_id` | — |
| 9. Classifier | langue, catégories, compagnies / marchés, scores heuristiques ; enrichissement LLM facultatif | junctions + scores | heuristiques seules |
| 10. Évaluer l'importance | règles (catégorie + autorité de la source + amplitude) | `importance_level` | — |
| 11. Résumer | LLM (filtre, budget, circuit-breaker) ou dégradé | `summary` articles | dégradé + erreur |
| 12. Enregistrer | upserts idempotents | base mise à jour | la transaction ne bloque pas le reste |
| 13. Digests | compagnie → marché → global (LLM ou dégradé) | `summary` + rapport | dégradé |
| 14. Rapport | `reports/YYYY-MM-DD.md` + `.json` | fichiers | rapport partiel consigné |
| 15. Bilan | `stats_json`, statut (`completed` / `partial`), purge 90 j, backup quotidien | base + backup | — |

**Échec partiel** : chaque échec est isolé par source / page / étape ; le run passe à `partial` dès la première erreur récupérable ; `invest-watch errors` liste tout ; `invest-watch run --resume <run_id>` ne re-traite que le manquant (idempotence des upserts) ; les erreurs `failed` repassent en `retrying` au run suivant (max 3 essais).

**Fuseaux et Chine** : le seuil J-1 est strict dans le fuseau utilisateur ; pour les compagnies chinoises, la tolérance ±6 h atténue la perte des publications matinales locales (décision Q5/D1).

## 11. Configuration

Format : **TOML** (`conf/config.toml`), chargé par `tomllib` (Python ≥ 3.11), versionnable et commenté. Structure type :

```toml
[app]
timezone = "Europe/Paris"
run_time = "07:00"
max_duration_minutes = 90
language = "fr"

[storage]
db_path = "data/invest.db"
reports_dir = "reports"
backups_dir = "backups"
logs_dir = "logs"
retention_days = 90
backup_enabled = true

[collect]
period_tolerance_hours = 6
min_content_chars = 200
max_response_bytes = 2000000
rate_limit_requests_per_sec = 0.5
max_queries_per_run = 40
queries_per_company = 3
queries_per_market = 2
fetch_user_agent = "invest-watch/1.0 (+contact@example.com)"
respect_robots = true
use_search_engines = true

[dedup]
title_similarity = 0.85
minhash_threshold = 0.9
event_window_hours = 24
seen_days = 7
new_event_lookback_days = 14

[scoring]
relevance_min = 0.5
importance_default = "moyenne"
importance_boost_categories = ["regulator", "financial_results"]
importance_boost_source_priority = [1, 2]

[llm]
enabled = true
model = "ibm/granite-3-8b-instruct"        # à vérifier au catalogue de votre région
endpoint = "https://us-south.ml.cloud.ibm.com"
project_id = "..."                          # ou space_id
max_input_chars = 8000
max_new_tokens = 700
temperature = 0.1
top_p = 0.9
timeout_seconds = 120
max_retries = 2
circuit_breaker_threshold = 3
max_calls_per_run = 150

[logging]
level = "INFO"
max_bytes = 5000000
backup_count = 5

# Sources : optionnel (gérées aussi en base via `invest-watch source add`)
[[sources]]
name = "HKEX News RSS"
type = "rss"
url = "https://www1.hkexnews.hk/rss/..."
lang = "en"
priority = 2

[[sources]]
name = "google-news-rss"
type = "search"
url = "https://news.google.com/rss/search?q={query}&hl={lang}"
lang = "*"
priority = 3

# Compagnies : optionnel (gérées aussi via `invest-watch company add`)
[[companies]]
name = "BYD"
ticker = "BYDDY"
isin = "CNE100000296"
country = "CN"
website = "https://www.byd.com"
alias = ["比亚迪"]
langue = ["zh", "en"]
[[companies.markets]]
name = "Véhicules électriques"
type = "industry"
```

**Secrets** : jamais en clair dans ce fichier — le config-manager résout `${WATSONX_API_KEY}` depuis l'environnement ou depuis le fichier `.env` restreint (`invest_watch/.env`, permissions 700, hors git) ; le trousseau OS (`keyring`, item `invest-watch/watsonx-api-key`) est l'alternative documentée. `conf/config.example.toml` fourni avec des valeurs fictives ; `conf/config.toml` et `.env` sont ignorés par git.


## 12. Exécution en tâche de fond

### 12.1 Recommandation

**Sur Windows (environnement cible) : Planificateur de tâches.** Tâche créée en fin d'installation (ou manuellement) :

```text
schtasks /Create /TN "invest-watch-daily" /TR "py -3 -m invest_watch run" /SC DAILY /ST 07:00 /F
```

- Démarrage même si l'utilisateur n'est pas connecté (compte dédié) ; condition réseau activée.
- **Alternatives documentées** : cron Linux/macOS (`0 7 * * * cd /… && python -m invest_watch run >> logs/cron.log 2>&1`).
- **Pas de processus permanent en V1** (ni service NSSM, ni `schedule`) : le run vit et meurt avec le processus invoqué — simple et sans fuite.

### 12.2 Isolation, reprise, vérification, arrêt

- **Anti-double-exécution** : verrou `data/run.lock` (`O_CREAT | O_EXCL`) ; second processus → sortie immédiate (code 0) ; verrou supprimé en fin de run ; `--force` pour outrepasser après vérification de l'horodatage.
- **Reprise d'une exécution interrompue** : `invest-watch run --date <YYYY-MM-DD> --resume <run_id>` relit `processing_error` (status `new` / `retrying`) ; les upserts idempotents empêchent les doublons ; les étapes coûteuses (LLM) ne sont rejouées que pour les articles non résumés.
- **Savoir si l'exécution a réussi** : code de sortie ≠ 0 en cas d'échec global, fichier `data/last_run.status`, table `daily_run.status`, rapport écrit.
- **Logs** : `logs/invest.log` (rotation) + `logs/run-YYYY-MM-DD.log` par exécution.
- **Heure et fuseau** : `run_time` + `timezone` dans le TOML ; l'application recalcule seule la période (J-1) — elle ne dépend pas du fuseau du planificateur.
- **Arrêt propre** : `invest-watch stop` écrit `data/stop.flag` ; le workflow vérifie le drapeau entre les étapes et sort en `cancelled` ; Ctrl-C géré (rollback de la transaction en cours, nettoyage du verrou).

## 13. Interface et format de sortie

### 13.1 Commandes CLI (V1)

| Commande | Rôle |
|---|---|
| `invest-watch run` | collecte quotidienne (manuel ou planificateur) |
| `invest-watch run --date <AAAAMMJJ> [--resume <run_id>]` | relance / reprise d'une journée |
| `invest-watch report [--date <AAAAMMJJ>]` | affiche le dernier rapport (ou celui d'une date) |
| `invest-watch company <nom>` | informations d'une compagnie |
| `invest-watch market <nom>` | informations d'un marché / secteur |
| `invest-watch status` | état de la base, dernier run, disponibilité LLM, verrou |
| `invest-watch errors [--limit N]` | erreurs récentes |
| `invest-watch export --format md/json --date <AAAAMMJJ>` | export lisible par l'humain |
| `invest-watch search "mots"` | recherche plein texte (FTS5) |
| `invest-watch company add/list/edit/remove` · `invest-watch market …` · `invest-watch source …` | gestion des entités |
| `invest-watch db migrate` | applique les migrations |
| `invest-watch gui` (V2) | lance le serveur de l'interface web locale (`127.0.0.1`) et ouvre le navigateur par défaut ; arrêt par Ctrl-C |

### 13.2 Structure du rapport quotidien Markdown

```text
# Veille Invest — Rapport du 2026-09-08

**Période couverte** : 2026-09-07 00:00 → 23:59:59 (Europe/Paris)
**Analyse LLM** : watsonx.ai (ibm/granite-3-8b-instruct, prompt v1.0) — disponible ✔
**Exécution** : run #142 · complété · 34 min · 2 erreurs mineures

## Résumé global du 2026-09-08
(3-5 phrases + « Points critiques » + « Points à vérifier »)

## 🚨 Informations critiques
| Compagnie | Titre | Impact (horizon) | Source | Vérif. |
| BYD | (titre) | négatif (court), conf. haute | [a12](url) | ✔ |

## Par compagnie
### BYD (CN)
**Faits rapportés** : … [a12] [a15]
**Conséquences potentielles** : …
**Points à vérifier** : …

## Par marché
### Véhicules électriques (secteur)
**Faits** : … [a12]
### Chine — régulation

## Articles importants du jour
| # | Date | Compagnie | Marché | Cat. | Score | Titre | Source |
| a12 | 07-09 | BYD | VE | regulator | 0.87 | … | [a12](url) |

## Informations non vérifiées / incertitudes
- (extraits de `summary.json_payload.incertitudes_ou_conflits` et `note_contradictions`)

## Erreurs & sources non accessibles
- flux « Reuters » : 429 après 2 tentatives ; page X : paywall (métadonnées seules).

> Rapport auto-généré — chaque fait est vérifiable dans sa source originale.
```

### 13.3 Règles d'affichage

- **Liens courts** : code local affiché `[a12]`, lien Markdown `[a12](url_originale)`, clé `"a12": "url"` en JSON.
- **Priorité d'affichage** : importance (critique → haute) → date → score.
- **Distinction explicite des trois registres** : ✅ fait rapporté (avec source) / 🔮 conséquence potentielle (horizon + confiance) / ❓ hypothèse ou élément non vérifié (liste dédiée).
- Informations contradictoires : champ `note_contradictions` repris dans la section « non vérifiées ».
- Chaque résumé affiche `model_id` / `prompt_version` en pied de page.


## 14. Sécurité, conformité et confidentialité

| Sujet | Mesure proposée |
|---|---|
| **Clés API** | Variable d'environnement / fichier `.env` restreint (hors git, permissions 700) — choix V1 ; trousseau OS (`keyring`) en alternative ; jamais dans `config.toml`, les logs ou les rapports ; rotation documentée |
| **Protection des données locales** | Base SQLite en local ; **aucun composant réseau entrant en V1** (en V2, interface web liée à `127.0.0.1` uniquement) ; droits de fichier restreints à l'utilisateur ; WAL ; option V2 : chiffrement SQLCipher ou dossier chiffré OS |
| **Interface web locale (V2)** | Serveur lié à `127.0.0.1` uniquement — aucune exposition sur le réseau ; pas d'accès distant ni d'authentification réseau (accès local) ; chaque écriture passe par les mêmes validations que la CLI (repositories) ; les consultations et éditions sont journalisées dans les logs ; aucune donnée envoyée à l'extérieur |
| **Données envoyées au LLM** | Minimum nécessaire (§8.2) : titre, date, source, texte tronqué, candidats ; troncature stricte ; **risque assumé et documenté** : le contenu transite vers IBM Cloud (région configurable, préférence UE) |
| **Règles de conservation** | Contenus textuels 90 jours (purge auto) ; métadonnées, extraits, résumés conservés ; backups 30 jours |
| **Conditions d'utilisation des sites** | Respect de robots.txt, User-Agent identifié, débit polit ; RSS et APIs officielles par défaut ; scraping de moteurs = recours configurable et documenté comme zone grise ; aucune technique interdite (pas de contournement de captchas ni de paywalls) |
| **Droits sur les contenus collectés** | Usage privé personnel, extraits courts, suppression automatique ; aucune redistribution |
| **Traçabilité des sources** | Chaque fait cite `#id` → article → URL originale → code court ; aucune information orpheline |
| **Risque de fausses informations** | Pas de confiance aveugle : statut `date_certaine`, `requires_human_check`, section « non vérifiées », détection de contradictions, score de confiance affiché |
| **Hallucination du LLM** | Prompts factuels (« ne rien inventer »), sortie JSON validée par schéma, références obligatoires ; champ absent → `null` ; mode dégradé produit des extraits littéraux uniquement |

## 15. Plan de tests

Cadre : `pytest` + `pytest-cov` (≥ 80 %), SQLite en mémoire par test, `responses` / `httpx-mock` pour le réseau, stub `LLMClient` partout ; fixtures pages HTML / RSS / JSON dans `tests/fixtures/`. Tous les tests s'exécutent **hors ligne et sans clé API**.

| Catégorie | Contenu testé | Critères de réussite |
|---|---|---|
| **Unitaires** | config_manager, normalisation URL, hash, parsing dates, scores, liens courts, purge | chemins attendus OK, bornes gérées (date inconnue, score hors bornes), aucune exception non gérée |
| **Interface web (V2)** | consultation (nouvelles par compagnie, résumés, marchés du jour et des jours précédents), édition des compagnies et marchés suivis, déclenchement de run, état du dernier run | routes testées via un client HTTP local ; aucune route accessible en dehors de `127.0.0.1` ; écritures conformes aux contraintes (repositories) ; page d'état du run cohérente |
| **Intégration (workflow)** | run complet avec sources mockées | base dans l'état attendu (daily_run, junctions, compteurs), rapport généré, aucun doublon d'article |
| **Données web simulées** | pages HTML variées (locales, multilingues, paywall partiel, dynamique, 404, 429) | extraction conforme, métadonnées justes, échecs classés, run non bloqué |
| **Déduplication** | URL variants, hash identiques, titres quasi identiques, reprises de dépêches, événement à 2 jours d'écart | table de vérité : chaque cas obtient le statut attendu (`duplicate` / `redundant` / `updated` / `new` / `complementary`) |
| **Filtrage par date** | dates explicites, relatives, fuseaux, tolérance ±6 h | sélection conforme ; `date_certaine` correct |
| **Génération de résumés** | golden tests des prompts, parsing JSON, validation de schéma | JSON valide et conforme ; réponse hors schéma → 1 tentative puis dégradé |
| **Indisponibilité LLM** | timeout, 5xx, quota, JSON invalide | circuit-breaker activé, mode dégradé, run `partial`, reprise au run suivant |
| **Indisponibilité d'une source** | flux mort, 403, flux mal formé | source isolée (`processing_error`), autres sources traitées |
| **Reprise après interruption** | kill du processus en plein run, `--resume` | reprise sans doublon ni perte, verrou levé, statut cohérent |
| **Planification** | invocation CLI telle que lancée par le planificateur | exit code attendu, `last_run.status` écrit |
| **Migration de base** | montée de schéma v1 → v2 | données préservées, `app_meta.db_version` à jour, migration idempotente |
| **Non-régression** | jeu complet exécuté à chaque intégration | zéro régression, couverture rapportée |
| **Réponses invalides du LLM** | JSON faux, contenu hors énumération, tokens épuisés | rejet contrôlé, retries bornés, dégradé sans plantage |


## 16. MVP — périmètre minimal viable

**Inclus** : configuration TOML ; CLI complète ; SQLite + migrations ; compagnies et alias (CLI) ; marchés hiérarchiques par défaut ; workflow quotidien complet (§10) ; cascade RSS → Google News RSS → DuckDuckGo (bascule `use_search_engines = false`) ; filtre J-1 ; trafilatura ; détection langue + date ; classification par règles ; scores heuristiques ; déduplication en 4 couches + regroupement d'événements ; résumés LLM watsonx.ai avec mode dégradé complet ; liens courts locaux ; rapports Markdown + JSON ; logs + erreurs + reprise ; purge 90 j ; backup quotidien ; tâche planifiée Windows ; plan de tests (§15).

**Exclu du MVP** (reporté) : interface graphique (§17), enrichissement LLM de la classification à confiance faible, recherche FTS5 avancée, export HTML, notifications, shortener externe, résumé par morceaux (chunking), Docker, multi-utilisateurs.

## 17. Évolutions futures

**V2 (autres améliorations) — feuille de route validée** : notifications (e-mail / Telegram) ; export HTML autonome ; reclassification LLM à confiance faible ; courbes de tendance par compagnie ; tableau de bord « sources dégradées » ; rapport hebdomadaire ; clés API Brave/Tavily configurables ; recherche plein texte FTS5 enrichie. À planifier après la validation du MVP.

**V2 — Interface graphique (décision utilisateur, forme à confirmer — D19)** : l'utilisateur a demandé une interface graphique permettant de **consulter les nouvelles de chaque société, les résumés et les marchés du jour et des jours précédents**, ainsi que de **changer les sociétés suivies** et de **vérifier / ajuster les marchés suivis**. Recommandation : **interface web locale interactive** (serveur lié à `127.0.0.1`, consultation via le navigateur, édition des compagnies/marchés via les mêmes repositories que la CLI), avec alternative d'application de bureau (PySide6/Qt). Voir §20 D19.

### Spécification fonctionnelle — interface graphique V2

**Forme et lancement**

- Interface **web locale interactive** : serveur lié à `127.0.0.1` (aucune exposition réseau), navigation via le navigateur.
- Framework web léger (recommandation : FastAPI — alternative : Flask), UI en HTML/CSS/JS, sans dépendance lourde de frontend.
- Commande : `invest-watch gui` lance le serveur et ouvre le navigateur par défaut ; arrêt propre par Ctrl-C (ou `invest-watch stop`).
- Rôle : complément de la CLI — la CLI reste disponible et prioritaire ; l'interface est un confort de consultation et d'édition.

**Consultation**

- **Nouvelles par société** : liste des articles du jour et des jours précédents (sélecteur de date), avec titre, date, source, langue, statut (nouveau / mis à jour / redondant), score de pertinence, niveau d'importance, catégorie et **lien court cliquable vers l'URL originale**.
- **Résumés** : résumé individuel de chaque article (faits, conséquences potentielles, incertitudes) et digests quotidiens par compagnie, par marché et global.
- **Marchés** : les marchés / secteurs / industries suivis, avec leurs articles associés pour la date sélectionnée.
- **État du système** : dernier run (statut, période couverte, durée, erreurs), sources actives, disponibilité LLM.

**Édition des compagnies suivies**

- Ajouter, modifier (nom, ticker, ISIN, pays, devise, site officiel, alias, langues, marché principal, actif/inactif) et supprimer des compagnies.
- Mêmes validations que la CLI (unicité des noms, ISIN, cohérence pays/marché).

**Vérification et ajustement des marchés suivis**

- Associer / dissocier des marchés à une compagnie (relation `company_market`).
- Ajouter un marché / secteur / industrie à la taxonomie (champs : nom, code, type, parent), ajuster la hiérarchie et l'activation.
- Ces modifications alimentent immédiatement les recherches du prochain run (rien ne nécessite de redémarrage).

**Autres actions**

- Bouton « Lancer une collecte » : déclenche un `invest-watch run` via l'orchestrateur en tâche de fond ; l'interface reste responsive et l'état du run est rafraîchi.
- Marquage utilisateur « à lire » / « à vérifier » sur un article (`article.flagged`).

**Sécurité de l'interface**

- Serveur lié à `127.0.0.1` uniquement — aucun accès depuis le réseau ; pas d'authentification réseau (accès local).
- Toute écriture passe par les repositories (contraintes et validations de la CLI) — jamais d'accès direct à la base côté HTML.
- Les consultations et éditions sont journalisées (logs) ; aucune donnée envoyée à l'extérieur.
- Non exposée : modification des sources (reste en CLI), multi-utilisateurs, API publique.

**Tests (voir §15)** : routage local, édition conforme aux contraintes, état du run cohérent, aucune exposition hors `127.0.0.1`.

**V3 (futures)** : traduction via LLM ; analyse de sentiment et reconnaissance d'entités (NER) ; alertes de seuils ; multi-utilisateurs ; Docker pour tests CI ; chiffrement SQLCipher ; liaison avec des données de marché (prix) distinctes du flux d'informations.

## 18. Critères d'acceptation

| # | Critère | Vérification |
|---|---|---|
| AC1 | Gestion de plusieurs compagnies | `invest-watch company add/list` avec ≥ 3 compagnies sans régression |
| AC2 | Une compagnie associée à plusieurs marchés | `company_market` avec ≥ 2 lignes pour une compagnie ; affichage par marché cohérent |
| AC3 | Collecte automatique le matin | planificateur invoque `invest-watch run` à 07h00 ; `daily_run` créé ; verrou anti-doublon vérifié |
| AC4 | Couverture par défaut de J-1 | `period_start` / `period_end` corrects dans le fuseau configuré (test avec J, J-1, J-2) |
| AC5 | Résultats dans un fichier de base | `data/invest.db` non vide après un run ; requêtes sur les repositories OK |
| AC6 | Texte et URLs originaux conservés | `article.url_original` identique à l'entrée source ; `article.content` = texte extrait de la fixture |
| AC7 | Sources présentées en liens très courts | `short_code` unique de 6-8 caractères ; rendu `[code](url)` présent dans le rapport |
| AC8 | Doublons détectés | jeu de 10 articles (4 doublons URL/hash, 3 titres identiques) → statuts exacts attendus |
| AC9 | Pas de répétition inutile des jours suivants | relance J-1 puis J : le rapport de J n'affiche que `new` / `updated` |
| AC10 | Rapports organisés par compagnie et par marché | sections présentes et cohérentes avec les données |
| AC11 | Résumés citant leurs sources | chaque `summary` contient des `#id` mappés sur des articles existants |
| AC12 | Sources gratuites configurables | `invest-watch source add/list/disable` ; changement de flux sans code |
| AC13 | Erreur d'une source isolée | 1 flux invalide + 1 flux OK → run `partial`, bonne source traitée |
| AC14 | Mode dégradé sans LLM | `llm.enabled = false` (et clé absente) : run complet, résumés dégradés, mode mentionné au rapport |
| AC15 | Secrets non en clair dans la config | `config.toml` ne contient jamais la clé ; requise via environnement / `.env` restreint (trousseau OS en alternative) au premier run |
| AC16 | Exécutions et erreurs tracées | `daily_run` et `processing_error` peuplés ; `stats_json` cohérent ; exit codes corrects |


## 19. Risques et mesures de réduction

| Risque | Probabilité / Impact | Réduction |
|---|---|---|
| Une API de recherche gratuite change ses conditions ou disparaît | Élevée / Moyen | Cascade de sources, sources configurables, quotas internes, fallback réseau documenté |
| Blocage d'IP (DuckDuckGo, agrégateurs) | Moyenne / Moyen | Débit polit + jitter, volumes faibles, bascule `use_search_engines` |
| Paywalls / évolution du HTML des pages | Élevée / Faible | Métadonnées seules, trafilatura tolérant, tests sur fixtures variées |
| Dérive du schéma / migration SQLite | Faible / Élevée | Migrations versionnées + tests de migration + backup avant montée |
| Coût LLM inattendu | Moyenne / Moyen | Bornes strictes, filtre de pertinence, troncature, cache, mode dégradé, métriques au rapport |
| Hallucination / biais du LLM | Moyenne / Élevée | Prompts factuels, schéma JSON, références obligatoires, section « non vérifiées », relecture humaine des points critiques |
| Non-conformité aux conditions d'utilisation d'un site | Moyenne / Moyen | RSS/APIs officielles par défaut, robots.txt, User-Agent identifié, aucune technique interdite |
| Exposition de données personnelles du portefeuille | Faible / Élevée | Données locales, chiffrement OS optionnel, purge, LLM limité au minimum requis |
| Reprise après crash corrompue | Faible / Moyen | Verrou + idempotence + upserts + backup quotidien `VACUUM INTO` |
| Pannes des sources institutionnelles (SEC, HKEXnews…) | Moyenne / Faible | Priorisation, isolation, métrique `last_success_at` / `error_count` des sources |
| Volumes explosifs (50 compagnies) | Moyenne / Moyen | Quotas, déduplication efficace, purge, FTS5 bornée |


## 20. Liste finale des décisions à prendre avant le développement

| # | Décision | Recommandation proposée | Valeur par défaut si non arbitrée |
|---|---|---|---|
| D1 | Fuseau de la « journée » | `Europe/Paris` | `Europe/Paris` |
| D2 | Heure de lancement | 07h00 | 07h00 |
| D3 | Nombre maximal de compagnies | 50 | 50 |
| D4 | Langues principales des sources | fr + en + zh | fr + en + zh |
| D5 | Type de base de données | SQLite | SQLite |
| D6 | Moteur de recherche gratuit principal | RSS + Google News RSS ; DuckDuckGo en secours | idem |
| D7 | API complémentaire payante (Brave / Tavily) | aucun achat en V1 | aucun |
| D8 | Modèle watsonx.ai et région | Granite instruct ; région UE préférée | à confirmer au catalogue à l'implémentation |
| D9 | Compte watsonx.ai (niveau / budget) | à vérifier (crédits gratuits ou abonnement) | sans compte : mode dégradé |
| D10 | Budget mensuel LLM | ≤ 5 € (réduit par bornes et cache) | borne strictement appliquée |
| D11 | Conservation des contenus | 90 jours | 90 jours |
| D12 | Langue et structure du rapport | français, structure §13 | idem |
| D13 | Export PDF | non en V1 | non |
| D14 | Notifications | non en V1 | non |
| D15 | Taxonomie marchés / secteurs | GICS + zones personnalisées | GICS + zones |
| D16 | Envoi du contenu au LLM accepté | oui, limité, explicite (opt-in dans le TOML) | opt-in obligatoire |
| D17 | Rétention des backups | 30 jours | 30 jours |
| D18 | Nom du projet / module Python | `invest_watch` (paquet `invest_watch/`) | `invest_watch` |
| D19 | **Forme de l'interface graphique V2** | **Interface web locale interactive** (serveur `127.0.0.1`, FastAPI ou Flask, HTML/CSS/JS) ; alternative : application de bureau PySide6/Qt | interface web locale interactive |

### Décisions arbitrées (2026-09-08)

- **§4.6 / §12** : planification par **Planificateur Windows (Task Scheduler)** retenu pour la V1 (cible Windows) ; cron conservé en documentation pour Linux/macOS.
- **§4.7 / §8.7 / §11 / §14 / AC15** : secrets par **variables d'environnement / fichier `.env` restreint** (hors git, permissions 700) ; trousseau OS (`keyring`) en alternative optionnelle.
- **§17** : la **feuille de route V2 (recommandées)** est retenue pour la période post-MVP ; une **interface graphique** doit être ajoutée à la V2 (consultation des nouvelles / résumés / marchés du jour et des jours précédents, gestion des sociétés suivies, vérification et ajustement des marchés suivis) — **forme recommandée : web locale interactive (D19, à confirmer)**.
- Toutes les autres options par défaut du document sont **validées telles quelles** par l'utilisateur.

### Remarques finales

- Ce document est **prêt à servir de base pour l'implémentation** : modèle de données (§9), workflow (§10), prompts (§8.3) et plan de tests (§15) sont écrits au niveau « spécification d'interface ».
- **Points à revérifier à la date d'implémentation** : le catalogue des modèles watsonx.ai et les paramètres du SDK `ibm-watsonx-ai` (D8), l'état actuel des niveaux gratuits des moteurs de recherche (D6/D7), et la **forme finale de l'interface graphique V2 (D19)**.
- Toute décision non arbitrée ci-dessus utilise la valeur par défaut indiquée — et chaque valeur est reproductible dans `config.toml`.

---

## 21. Spécifications détaillées des fonctions avancées (A1–A3, B1–B2, B5)

> Ces fonctions constituent le premier lot d'évolutions post-MVP. Elles restent 100 % locales et s'intègrent dans l'architecture existante (même base SQLite, même CLI, même workflow).

---

### 21.A1 — Calendrier des catalyseurs (F22)

**Objectif** : anticiper les mouvements de marché en affichant, par compagnie, les prochains événements datés susceptibles de faire bouger le cours.

**Sources** : flux RSS des pages Relations Investisseurs officielles + agendas institutionnels publics (tout gratuit, aucun LLM requis).

**Types d'événements suivis**

| Catégorie | Exemples concrets |
|---|---|
| Résultats financiers | Publication trimestrielle / semestrielle / annuelle |
| Ventes mensuelles | BYD, SAIC, Geely et autres constructeurs chinois (chiffres mensuels publiés ~5 du mois) |
| Salons et conférences | CES (janvier), IAA (septembre), Auto Shanghai / Beijing (alternance annuelle), Battery Show |
| Échéances réglementaires | Dates limites de dépôt SEC/HKEX/CSRC, mises à jour des quotas d'émission CO₂ |
| Expirations d'accords | Fin de partenariats, licences, accords de fournisseurs signalés dans les communiqués précédents |

**Option « à surveiller demain »** : chaque soir, la section `DEMAIN` du rapport liste les événements dont la date tombe dans les prochaines 24 h pour les compagnies actives.

**Implémentation**

- Nouvelle table `catalyst_event` : `id`, `company_id` FK, `event_type` TEXT, `event_date` TEXT (UTC), `title`, `source_url`, `source_id` FK, `confirmed` INTEGER (0 = estimé, 1 = date officielle), `notes`, `created_at`, `updated_at`.
- Alimentation : extraction automatique lors du traitement des articles (regex + LLM optionnel sur les communiqués IR) + saisie CLI manuelle (`invest-watch catalyst add/list/delete`).
- Affichage CLI : `invest-watch catalysts [--company X] [--days 7]` ; section dédiée dans le rapport Markdown quotidien.
- **Complexité estimée** : Moyenne — flux RSS officiels IR (gratuits), aucun LLM obligatoire.

---

### 21.A2 — Détection de pics de mentions / momentum (F23)

**Objectif** : détecter qu'il « se passe quelque chose » sur une compagnie avant même de lire les articles, grâce à un signal purement statistique.

**Algorithme (zéro LLM, purement heuristique)**

1. Pour chaque compagnie active, compter le nombre d'articles collectés dans la fenêtre J-1 (24 h) → `count_24h`.
2. Calculer la moyenne mobile sur 30 jours → `baseline_30d` (stockée dans `app_meta` ou table dédiée).
3. Si `count_24h ≥ baseline_30d × seuil_alerte` (défaut : `2.5`, configurable dans `[momentum]` du TOML) → alerte `MOMENTUM` dans le rapport.
4. Le niveau de pic est gradué : `×2` → « activité inhabituelle », `×5` → « pic fort », `×10` → « pic exceptionnel ».

**Modèle de données** : table `mention_stats` — `company_id` FK, `stat_date` TEXT, `count_24h` INTEGER, `baseline_30d` REAL, `ratio` REAL, `alert_triggered` INTEGER ; UNIQUE (company_id, stat_date).

**Affichage** : encadré `⚡ MOMENTUM` en tête de section compagnie dans le rapport si `alert_triggered = 1` ; `invest-watch momentum [--company X]` affiche l'historique.

**Complexité estimée** : Faible — calculs arithmétiques simples, aucune dépendance externe.

---

### 21.A3 — Vérification croisée des chiffres entre sources (F24)

**Objectif** : détecter les écarts factuels entre différentes sources couvrant le même événement (ex. « Reuters : 1,2 Md$ vs Bloomberg : 1,5 Md$ »), offrant directement la valeur analytique pour un investisseur.

**Déclenchement** : sur chaque cluster d'événements (`event_id` identique, ≥ 2 articles) incluant au moins un article à `importance_level` ≥ `haute`.

**Prompt dédié (1 appel LLM par cluster)**

```
Rôle : analyste financier rigoureux.
Tâche : extraire tous les chiffres clés (montants, %, capacités, volumes, dates) de chacun des {n} articles ci-dessous et signaler tout écart ≥ 5 % ou toute contradiction entre sources.
Format de réponse JSON :
{
  "figures": [{"label": "...", "values": [{"source": "...", "value": "...", "unit": "..."}]}],
  "discrepancies": [{"label": "...", "detail": "..."}],
  "confidence": "high|medium|low"
}
```

- Maîtrisé par les bornes LLM existantes (§8.6) : ne s'exécute que si `count(cluster) ≤ max_articles_per_cluster` (défaut 6) et `importance ≥ haute`.
- Résultat stocké dans `summary` (type `cross_check`, ref_id = event_id du cluster).
- Affichage : bloc `⚠ ÉCARTS DÉTECTÉS` dans le rapport si `discrepancies` non vide.

**Complexité estimée** : Moyenne — +1 appel LLM par cluster concerné, maîtrisé par les bornes existantes.

---

### 21.B1 — Thèse d'investissement par compagnie (F25)

**Objectif** : relier chaque news directement à la position de l'investisseur en marquant automatiquement chaque article « conforte / contredit / neutre par rapport à ma thèse ».

**Structure de la thèse (champs locaux, jamais envoyés au LLM sauf opt-in explicite)**

| Champ | Type | Description |
|---|---|---|
| `thesis_bull` | TEXT | Scénario haussier personnel (raisons d'acheter / conserver) |
| `thesis_bear` | TEXT | Scénario baissier / risques identifiés |
| `exit_threshold_up` | TEXT | Seuil ou condition de sortie en hausse |
| `exit_threshold_down` | TEXT | Seuil ou condition de sortie en baisse |
| `last_reviewed_at` | TEXT | Date de dernière relecture humaine |

Ces champs sont ajoutés à la table `company` (migration versionnée).

**Marquage automatique des articles**

- **Règle heuristique (sans LLM)** : comparaison par mots-clés extraits de `thesis_bull` / `thesis_bear` avec le titre + extrait de l'article → marquage rapide `thesis_signal` IN ('conforte','contredit','neutre').
- **Enrichissement LLM (opt-in, `thesis.use_llm = true`)** : 1 appel LLM par article à `importance_level ≥ haute` avec un prompt court résumant la thèse + l'extrait de l'article → réponse JSON `{"signal": "conforte|contredit|neutre", "reason": "..."}`.
- La thèse n'est **jamais envoyée au LLM** si `thesis.use_llm = false` (défaut).

**Modèle de données** : colonne `thesis_signal` TEXT + `thesis_reason` TEXT ajoutées à `article_company` ; CHECK IN ('conforte','contredit','neutre',NULL).

**CLI** : `invest-watch thesis set <company> [--bull "..." --bear "..." --exit-up "..." --exit-down "..."]` ; `invest-watch thesis show <company>` ; `invest-watch thesis review` liste les articles « contredit » récents.

**Affichage** : dans le rapport, chaque article d'une compagnie ayant une thèse est étiqueté `[✓ conforte]` / `[✗ contredit]` / `[~ neutre]` ; section « Points qui contredisent ma thèse » en fin de section compagnie si ≥ 1 article `contredit`.

**Complexité estimée** : Moyenne (1 appel LLM par article pertinent en mode activé, ou règle simple sans LLM) — 100 % local.

---

### 21.B2 — Tableau de bord des drivers macro/sectoriels (F26)

**Objectif** : surveiller les indicateurs macroéconomiques et sectoriels qui conditionnent les décisions d'investissement, en particulier pour le secteur VE / Chine.

#### Indicateurs suivis

**Matières premières** (prix spot ou indice public)

| Indicateur | Source gratuite recommandée |
|---|---|
| Prix du lithium (carbonate, hydroxyde) | Shanghai Metals Market (SMM) RSS / Fastmarkets communiqués publics |
| Prix du cuivre (LME) | LME communiqués publics / Reuters commodities RSS |
| Prix des terres rares (néodyme, praséodyme…) | SMM RSS / USGS communiqués |
| Prix de l'acier | Mysteel communiqués / World Steel Association |

**Chine — macro et secteur VE**

| Indicateur | Source institutionnelle gratuite |
|---|---|
| Ventes mensuelles VE en Chine (totales + BEV/PHEV) | **CAAM** — Association des constructeurs automobiles de Chine, publication ~5 du mois |
| PMI manufacturier et non-manufacturier | **NBS** — Bureau national des statistiques (publication mensuelle) |
| IPC (inflation) | **NBS** — publication mensuelle |
| Décisions de taux / RRR / LPR | **PBoC** — Banque populaire de Chine, communiqués officiels |

**Autres régions — macro**

| Indicateur | Source |
|---|---|
| Taux directeurs (Fed, BCE, BoE, BoJ) | Communiqués officiels des banques centrales (RSS) |
| PMI mondial / zone euro / États-Unis | S&P Global communiqués PMI (RSS) |
| Taux obligataires clés (10 ans US, DE, CN) | Données publiques des ministères des finances / banques centrales |

#### Flux institutionnels chinois gratuits (détail)

| Sigle | Nom complet | Contenu | URL type |
|---|---|---|---|
| **NBS** | Bureau national des statistiques de Chine *(Guojia Tongjiju)* | IPC, IPP, PIB, PMI officiel, population ; conférences de presse de Jiang Jun | `stats.gov.cn` — flux RSS + pages de communiqués |
| **PBoC** | Banque populaire de Chine *(Zhongguo Renmin Yinhang)* | Taux LPR, RRR, opérations d'open market, communiqués de politique monétaire | `pbc.gov.cn` — communiqués officiels |
| **CSRC** | Commission de réglementation des valeurs mobilières de Chine *(Zhongguo Zhengjianhui)* | Sanctions, nouvelles règles, approbations d'introduction en bourse, circulaires | `csrc.gov.cn` |
| **SHSE** | Bourse de Shanghai *(Shanghai Zhengquan Jiaoyisuo)* | Divulgations des sociétés cotées (A-shares), avis, suspensions | `sse.com.cn` |
| **SZSE** | Bourse de Shenzhen *(Shenzhen Zhengquan Jiaoyisuo)* | Divulgations des sociétés cotées (A-shares et ChiNext/STAR) | `szse.cn` |
| **HKEXnews** | Dépôt réglementaire de la Bourse de Hong Kong | Divulgations des sociétés cotées à Hong Kong (H-shares, Red Chips) ; flux par ticker | `hkexnews.hk` — API REST + flux RSS par compagnie |
| **CAAM** | Association des constructeurs automobiles de Chine *(Zhongguo Qiche Gongye Xiehui)* | Ventes mensuelles VE (BEV + PHEV), production, exportations, par constructeur | `caam.org.cn` — communiqués mensuels |
| **Caixin Global** | Médias Caixin — édition anglaise | PMI Caixin (indépendant du NBS), analyses macro, secteur financier chinois | `caixin.com` — flux RSS public |
| **SCMP** | South China Morning Post | Actualité politique, économique et réglementaire Chine / Hong Kong | `scmp.com` — flux RSS public |

> **Intégration** : ces sources sont déclarées dans la table `source` avec `type = 'rss'` ou `type = 'api'`, `lang = 'zh'` ou `'en'`, priorité rang 2. Elles alimentent le tableau de bord macro (F26) et la collecte standard (F4) selon leur nature.

#### Implémentation

- Nouvelle table `macro_indicator` : `id`, `indicator_key` TEXT UNIQUE, `label`, `value` TEXT, `unit`, `period` TEXT (ex. « 2025-05 »), `source_id` FK, `published_at` TEXT (UTC), `collected_at`, `created_at`.
- Le workflow quotidien interroge les sources macro configurées et insère / met à jour les indicateurs (`upsert` sur `indicator_key + period`).
- CLI : `invest-watch macro [--indicator lithium] [--since 2025-01]` ; tableau récapitulatif dans le rapport quotidien (section `DRIVERS MACRO`).
- **Complexité estimée** : Moyenne — flux institutionnels gratuits, structure simple, aucun LLM requis.

---

### 21.B5 — Sentiment & momentum 30-90 j (F27)

**Objectif** : fournir une vue temporelle du ton éditorial et du volume de couverture par compagnie / marché, sur une fenêtre glissante de 30 à 90 jours, pour détecter les tendances de fond invisibles au quotidien.

**Deux composantes**

| Composante | Source de données | Méthode |
|---|---|---|
| **Volume de mentions** | `mention_stats.count_24h` (calculé par F23) | Courbe historique 30-90 j, moyenne mobile 7 j |
| **Ton moyen (sentiment)** | Score de sentiment extrait des résumés LLM existants (`summary.json_payload.sentiment`) | Moyenne pondérée par `relevance_score` sur la fenêtre, normalisée −1..+1 |

**Enrichissement du prompt de résumé existant (§8.3)** : ajouter le champ `"sentiment": "positif|neutre|négatif"` + `"sentiment_score": float −1..1` dans le JSON de sortie de F12 (résumé individuel). Coût marginal : 0 token supplémentaire si le champ est inclus dans la contrainte de format JSON existante.

**Modèle de données** : table `sentiment_history` — `company_id` FK (ou `market_id` FK), `stat_date` TEXT, `sentiment_avg` REAL, `sentiment_count` INTEGER, `volume_7d_avg` REAL ; UNIQUE (company_id/market_id, stat_date).

**Affichage** : greffé sur l'interface graphique V2 (§17) — courbe interactive sentiment + volume sur 30/60/90 j ; en V1 CLI : `invest-watch sentiment <company> [--days 90]` retourne un tableau ASCII avec les valeurs hebdomadaires.

**Complexité estimée** : Moyenne — se greffe sur les données et résumés existants ; la courbe graphique est V2, le calcul et le stockage sont activables dès la V1.

---

*Fin du document — invest-watch, spécifications fonctionnelles et techniques v1.3.*