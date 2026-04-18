# CloudPrice Compass

> Comparateur multi-cloud en temps réel — AWS, GCP, Azure — sur 1 000+ instances compute.

**Live** : [cloudprice-compass.onrender.com/app](https://cloudprice-compass.onrender.com/app)  
**Stack** : Python · DuckDB · dbt · FastAPI · HTML/CSS/JS · Render

---

## Ce que fait ce projet

CloudPrice Compass répond à une question simple : *pour un besoin précis (RAM, CPU, budget, use case), quelle instance cloud offre le meilleur rapport qualité-prix ?*

Le comparateur ingère les prix en temps réel depuis les APIs publiques des trois grands providers, les normalise dans un schéma DuckDB unifié via dbt, et expose un moteur de recherche avec scoring dynamique selon le use case. Un frontend HTML minimaliste permet de rechercher, filtrer et comparer jusqu'à 4 instances côte à côte.

---

## Architecture

```
APIs publiques
  ├── instances.vantage.sh          → AWS EC2 pricing (JSON, ~3MB)
  ├── GCP Cloud Billing API         → GCP SKUs (31k entrées, paginé)
  └── Azure Retail Prices API       → Azure VMs (51k entrées, paginé)
         ↓
   ingest/*.py (Python + httpx)
         ↓
   DuckDB (raw_aws_instances / raw_gcp_instances / raw_azure_instances)
         ↓
   dbt (staging views → mart_instances table)
         ↓
   FastAPI (/search · /compare · /stats)
         ↓
   frontend/index.html
```

---

## Structure du repo

```
cloudprice-compass/
├── ingest/
│   ├── aws_pricing.py        # Fetch + parse instances.vantage.sh
│   ├── gcp_pricing.py        # Fetch Cloud Billing API + reconstruit instances depuis SKUs CPU/RAM
│   └── azure_pricing.py      # Fetch Azure Retail Prices API + parse specs depuis noms ARM
├── transforms/
│   ├── dbt_project.yml
│   ├── profiles.yml
│   └── models/
│       ├── staging/
│       │   ├── sources.yml
│       │   ├── stg_aws_instances.sql
│       │   ├── stg_gcp_instances.sql
│       │   └── stg_azure_instances.sql
│       └── marts/
│           └── mart_instances.sql
├── api/
│   ├── main.py               # FastAPI endpoints
│   └── search.py             # Moteur de recherche + logique de scoring
├── frontend/
│   ├── index.html            # Interface complète (vanilla HTML/CSS/JS)
│   └── favicon.svg
├── data/
│   └── compass.duckdb        # DB pré-générée committée (2.1MB)
├── start.sh                  # Script de démarrage Render
├── render.yaml
└── requirements.txt
```

---

## Sources de données

| Provider | Source | Auth | Format | Volume |
|----------|--------|------|--------|--------|
| AWS | [instances.vantage.sh](https://instances.vantage.sh/instances.json) | Aucune | JSON | ~3MB, 1 239 instances |
| GCP | [Cloud Billing API](https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus) | Clé API gratuite | JSON paginé | 31 042 SKUs |
| Azure | [Retail Prices API](https://prices.azure.com/api/retail/prices) | Aucune | JSON paginé | 51 000+ items |

### Pourquoi ces sources et pas les APIs officielles AWS ?

L'API officielle AWS Pricing retourne un fichier JSON de ~600MB pour EC2 seul. `instances.vantage.sh` (maintenu par Vantage) expose les mêmes données dans un format 200x plus léger. GCP et Azure ont des APIs publiques bien documentées et légères.

---

## Modélisation des données

### Différence fondamentale entre providers

**AWS** vend des instances complètes avec prix fixe : `r6g.large` = 2 vCPU / 16GB / 0.1128$/h.

**GCP** vend des composants séparés : un SKU pour les vCPU, un SKU pour la RAM. Le prix d'une instance `n2-standard-4` se calcule :
```
prix = (4 × prix_vcpu_n2) + (16 × prix_ram_n2)
     = (4 × 0.034802) + (16 × 0.004664)
     = 0.139208 + 0.074624
     = 0.213832 $/h
```
Cette architecture permet les **custom machine types** — tu peux commander exactement 6 vCPU et 11GB RAM.

**Azure** fournit les prix par instance mais pas les specs hardware (vcpu/RAM) dans la même API. Les specs sont parsées depuis les noms ARM selon la convention de nommage Azure.

### Convention de nommage Azure

```
Standard_D4s_v3
         │││ │└─ version (v3)
         │││ └── premium storage (s)
         ││└──── taille (4 → vcpu count pour les séries Dv3+)
         │└───── série (D = general purpose)
         └────── Standard (toutes les VMs compute)
```

Familles principales :

| Lettre | Série | RAM/vCPU | Usage |
|--------|-------|----------|-------|
| D | General purpose | ×4 GB | Polyvalent |
| E | Memory optimized | ×8 GB | Bases de données, Redis |
| F | Compute optimized | ×2 GB | Web servers, calcul |
| B | Burstable | ×4 GB | Dev, workloads intermittents |
| L | Storage optimized | ×8 GB | NoSQL, data warehousing |
| H | HPC | ×8 GB | Calcul scientifique |
| p (suffixe) | ARM processor | — | Équivalent Graviton AWS |

**Cas particulier D11–D15** : le nombre dans le nom est un numéro de série, pas un vcpu count. `D11_v2` = 2 vCPU / 14GB, pas 11 vCPU. Ces cas sont gérés par une table de specs manuelles dans `azure_pricing.py`.

### Convention de nommage AWS

```
r6g.large
│││  └─── taille (medium/large/xlarge/2xlarge...)
││└────── processeur (g=Graviton ARM, a=AMD, i=Intel, d=NVMe local)
│└─────── génération (6 = plus récent que 5)
└──────── famille (r=memory, c=compute, m=general, t=burstable...)
```

Familles AWS mappées en catégories :

| Famille | Catégorie |
|---------|-----------|
| r, x, u | memory-optimized |
| c | compute-optimized |
| m, t, a | general-purpose |
| i, d | storage-optimized |

### Schema DuckDB unifié

```sql
-- mart_instances (table dbt finale)
instance_id       VARCHAR   -- "aws:r6g.large"
provider          VARCHAR   -- aws / gcp / azure
instance_type     VARCHAR   -- r6g.large
family            VARCHAR   -- r (lettre seule, normalisée)
category          VARCHAR   -- memory-optimized / general-purpose / ...
vcpu              INTEGER
memory_gb         FLOAT
region            VARCHAR   -- eu-west-1 / europe-west4 / westeurope
price_ondemand    FLOAT     -- $/h
price_reserved    FLOAT     -- $/h reserved 1 an (null si indisponible)
reserved_savings_pct FLOAT  -- % d'économie reserved vs on-demand
price_per_vcpu    FLOAT     -- prix normalisé par vCPU
price_per_gb_ram  FLOAT     -- prix normalisé par GB RAM
value_score       FLOAT     -- score composite (varie selon use case)
```

---

## Moteur de recherche et scoring

Le `value_score` est recalculé dynamiquement selon le use case sélectionné. Il combine `price_per_vcpu` et `price_per_gb_ram` avec des poids différents :

| Use case | Poids CPU | Poids RAM | Logique |
|----------|-----------|-----------|---------|
| general | 50% | 50% | Équilibré |
| postgres | 20% | 80% | RAM critique pour buffer pool |
| redis | 10% | 90% | Tout en RAM |
| web | 70% | 30% | Concurrence CPU-bound |
| ml | 60% | 40% | Inférence CPU-intensive |
| spark | 50% | 50% | Équilibré — shuffle en RAM/disque |

Plus le score est bas, meilleur est le rapport qualité-prix pour ce use case.

---

## API endpoints

```
GET /search
  ?ram_min=16&ram_max=64
  &vcpu_min=4
  &price_max=0.5
  &use_case=postgres
  &providers=aws,gcp,azure
  &limit=20

GET /compare
  ?ids=aws:r6g.large,gcp:n2-standard-2,azure:e2s-v5

GET /stats
GET /app          → frontend HTML
```

---

## Lancer le projet en local

```bash
git clone https://github.com/Vanelfokamcode/cloudprice-compass.git
cd cloudprice-compass

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Copie .env.example en .env et ajoute ta clé GCP
cp .env.example .env

# La DB est déjà committée — tu peux lancer directement
uvicorn api.main:app --reload

# Ouvre http://localhost:8000/app
```

Pour regénérer la DB depuis les APIs :

```bash
python ingest/aws_pricing.py
python ingest/gcp_pricing.py
python ingest/azure_pricing.py

cd transforms && dbt run --profiles-dir . && cd ..
```

### Clé GCP gratuite

1. Va sur [console.cloud.google.com](https://console.cloud.google.com)
2. Crée un projet → APIs & Services → Library → active **Cloud Billing API**
3. Credentials → Create credentials → API Key
4. Restreins la clé à **Cloud Billing API**

---

## Ce que j'ai appris en construisant ce projet

**Ingestion multi-source hétérogène** — les trois providers ont des formats radicalement différents. AWS expose un JSON plat par instance, GCP vend des composants à assembler, Azure nécessite de parser les specs depuis les conventions de nommage. Normaliser ça en un schéma unique est le vrai travail DE.

**Pagination et streaming** — le fichier AWS global fait 600MB. L'API GCP pagine sur 31 000 SKUs. L'API Azure retourne 51 000 items sans filtre efficace. Gérer ça proprement (cache local, retry, limite de pagination) est indispensable en production.

**Idempotence** — tous les scripts utilisent `INSERT OR REPLACE` pour être relançables sans corrompre les données. Le principe : un pipeline qu'on peut relancer 10 fois doit donner le même résultat que si on l'avait lancé une fois.

**dbt staging → mart** — séparer le nettoyage source par source (staging views) de l'assemblage final (mart table) rend le pipeline maintenable. Si Azure change son format, on ne touche qu'à `stg_azure_instances.sql`.

**Scoring dynamique** — le même dataset peut répondre à des questions différentes selon les poids appliqués. Un comparateur qui retourne la même réponse pour "je fais du PostgreSQL" et "je fais du machine learning" est un mauvais comparateur.

**Déploiement contraint** — Render free tier = 512MB RAM. L'ingestion complète dépasse ça. Solution : committer la DB pré-générée (2.1MB) et skip l'ingestion au démarrage. Pattern courant en production pour les datasets qui changent rarement.

---

## Données indexées

- **AWS** : 974 instances EC2 Linux, région eu-west-1
- **GCP** : 26 machine types Compute Engine, région europe-west4
- **Azure** : ~79 VMs Linux, région westeurope

L'écart AWS/GCP/Azure reflète les choix d'ingestion (AWS via catalogue complet Vantage, GCP via liste hardcodée de machine types, Azure via parsing des APIs).

---

## Stack technique

| Couche | Outil |
|--------|-------|
| Ingestion | Python 3.12, httpx, requests |
| Stockage | DuckDB 1.5.2 |
| Transformation | dbt-duckdb 1.10.1 |
| API | FastAPI 0.111, uvicorn |
| Frontend | HTML/CSS/JS vanilla, IBM Plex Mono |
| Déploiement | Render (free tier) |

---

*Projet portfolio — Vanel Fokam · 2026*  
*GitHub : [Vanelfokamcode/cloudprice-compass](https://github.com/Vanelfokamcode/cloudprice-compass)*