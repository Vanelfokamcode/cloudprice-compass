import httpx
import duckdb
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
REGION = "europe-west4"
DB_PATH = Path("data/compass.duckdb")
CACHE_PATH = Path("data/gcp_skus_raw.json")
GCP_API_KEY = os.getenv("GCP_API_KEY")
SERVICE_ID = "6F81-5844-456A"

# Machines GCP prédéfinies qu'on veut reconstruire
# Format : (famille, vcpu, memory_gb)
GCP_MACHINE_TYPES = [
    # N2 — general purpose Intel
    ("n2-standard-2",   "n2",  2,   8.0),
    ("n2-standard-4",   "n2",  4,  16.0),
    ("n2-standard-8",   "n2",  8,  32.0),
    ("n2-standard-16",  "n2", 16,  64.0),
    ("n2-standard-32",  "n2", 32, 128.0),
    ("n2-highmem-2",    "n2",  2,  16.0),
    ("n2-highmem-4",    "n2",  4,  32.0),
    ("n2-highmem-8",    "n2",  8,  64.0),
    # N2D — general purpose AMD
    ("n2d-standard-2",  "n2d",  2,   8.0),
    ("n2d-standard-4",  "n2d",  4,  16.0),
    ("n2d-standard-8",  "n2d",  8,  32.0),
    ("n2d-standard-16", "n2d", 16,  64.0),
    # C3 — compute optimized
    ("c3-standard-4",   "c3",  4,  16.0),
    ("c3-standard-8",   "c3",  8,  32.0),
    ("c3-standard-22",  "c3", 22,  88.0),
    # E2 — cost optimized
    ("e2-standard-2",   "e2",  2,   8.0),
    ("e2-standard-4",   "e2",  4,  16.0),
    ("e2-standard-8",   "e2",  8,  32.0),
    ("e2-standard-16",  "e2", 16,  64.0),
    ("e2-highmem-2",    "e2",  2,  16.0),
    ("e2-highmem-4",    "e2",  4,  32.0),
    ("e2-highmem-8",    "e2",  8,  64.0),
    # T2D — scale-out AMD
    ("t2d-standard-1",  "t2d",  1,   4.0),
    ("t2d-standard-2",  "t2d",  2,   8.0),
    ("t2d-standard-4",  "t2d",  4,  16.0),
    ("t2d-standard-8",  "t2d",  8,  32.0),
]


def fetch_all_skus() -> list:
    """
    Fetch tous les SKUs Compute Engine via l'API GCP.
    L'API est paginée — on suit le nextPageToken jusqu'à épuisement.

    Pourquoi on cache : l'API retourne ~15000 SKUs, c'est long à fetcher.
    En dev on relit le cache local.
    """
    if CACHE_PATH.exists():
        print(f"Cache trouvé — lecture locale")
        return json.loads(CACHE_PATH.read_bytes())

    base_url = f"https://cloudbilling.googleapis.com/v1/services/{SERVICE_ID}/skus"
    all_skus = []
    page_token = None
    page = 1

    while True:
        params = {"key": GCP_API_KEY, "pageSize": 5000}
        if page_token:
            params["pageToken"] = page_token

        print(f"Fetching page {page}...")
        r = httpx.get(base_url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        skus = data.get("skus", [])
        all_skus.extend(skus)
        print(f"  → {len(skus)} SKUs (total: {len(all_skus)})")

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        page += 1

    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(all_skus))
    print(f"Cached {len(all_skus)} SKUs")
    return all_skus


def nanos_to_float(units: str, nanos: int) -> float:
    """
    Convertit le format de prix GCP en float.

    GCP évite les floats en séparant la partie entière (units)
    et la partie décimale en nanounités (nanos).

    Ex: units="0", nanos=31611000 → 0.031611 $/h
    Ex: units="1", nanos=500000000 → 1.5 $/h
    """
    return float(units) + nanos / 1_000_000_000


def extract_component_prices(skus: list) -> dict:
    """
    Extrait les prix CPU et RAM par famille de machine pour notre région.

    Retourne un dict :
    {
      "n2":  {"cpu": 0.031611, "ram": 0.004237},
      "n2d": {"cpu": 0.024695, "ram": 0.003313},
      ...
    }

    Logique de filtrage :
    - serviceRegions doit contenir notre REGION
    - category.usageType == "OnDemand"
    - category.resourceFamily == "Compute"
    - description contient le nom de la famille (ex: "N2 Instance Core")
    """
    prices = {}

    for sku in skus:
        # Filtre région
        if REGION not in sku.get("serviceRegions", []):
            continue

        category = sku.get("category", {})

        # Filtre OnDemand uniquement
        if category.get("usageType") != "OnDemand":
            continue

        # Filtre Compute uniquement
        if category.get("resourceFamily") != "Compute":
            continue

        desc = sku.get("description", "").lower()

        # Filtre : on veut pas les Sole Tenancy, Spot, Preemptible, Custom
        if any(x in desc for x in ["sole tenancy", "sole tenant", "custom", "premium"]):
            continue

        # Extrait le prix depuis tieredRates
        try:
            pricing_info = sku["pricingInfo"][0]["pricingExpression"]
            rate = pricing_info["tieredRates"][0]["unitPrice"]
            price = nanos_to_float(rate.get("units", "0"), rate.get("nanos", 0))
        except (KeyError, IndexError):
            continue

        if price == 0:
            continue

        resource_group = category.get("resourceGroup", "")

        # Identifie la famille depuis la description
        # "N2 Instance Core running in Netherlands" → famille = "n2"
        # "N2D AMD Instance Core running in Netherlands" → famille = "n2d"
        family = _identify_family(desc, resource_group)
        if not family:
            continue

        if family not in prices:
            prices[family] = {}

        # CPU SKU
        if resource_group == "CPU" and "cpu" not in prices[family]:
            prices[family]["cpu"] = price

        # RAM SKU
        if resource_group == "RAM" and "ram" not in prices[family]:
            prices[family]["ram"] = price

    return prices


def _identify_family(desc: str, resource_group: str) -> str | None:
    """
    Identifie la famille de machine depuis la description du SKU.
    Retourne None si on ne reconnaît pas la famille.
    """
    if resource_group not in ("CPU", "RAM"):
        return None

    # Ordre important : les plus spécifiques d'abord
    families = [
        ("n2d", "n2d"),
        ("n2",  "n2 "),
        ("n4",  "n4 "),
        ("c3d", "c3d"),
        ("c3",  "c3 "),
        ("c4",  "c4 "),
        ("e2",  "e2 "),
        ("t2d", "t2d"),
        ("t2a", "t2a"),
        ("m3",  "m3 "),
        ("m2",  "m2 "),
        ("m1",  "m1 "),
    ]

    for family_key, pattern in families:
        if pattern in desc:
            return family_key

    return None


def build_instances(component_prices: dict) -> list[dict]:
    """
    Reconstruit les instances prédéfinies en combinant prix CPU + RAM.

    C'est la différence fondamentale avec AWS :
    price_instance = (vcpu × price_cpu) + (memory_gb × price_ram)
    """
    rows = []

    for instance_type, family, vcpu, memory_gb in GCP_MACHINE_TYPES:
        if family not in component_prices:
            print(f"  Famille {family} non trouvée dans les SKUs — skip")
            continue

        comp = component_prices[family]
        cpu_price = comp.get("cpu")
        ram_price = comp.get("ram")

        if not cpu_price or not ram_price:
            print(f"  Prix incomplet pour {family} — skip")
            continue

        # Le calcul clé — c'est ce qu'on a fait à la main avant de coder
        price_ondemand = round((vcpu * cpu_price) + (memory_gb * ram_price), 6)

        # Famille courte pour l'affichage : "n2-standard-8" → "n2"
        family_display = instance_type.split("-")[0]

        rows.append({
            "instance_type":  instance_type,
            "family":         family_display,
            "provider":       "gcp",
            "vcpu":           vcpu,
            "memory_gb":      memory_gb,
            "os":             "linux",
            "region":         REGION,
            "price_ondemand": price_ondemand,
            "price_reserved": None,  # GCP appelle ça CUD — on ajoutera Jour 3
        })

    return rows


def load_to_duckdb(rows: list[dict]) -> None:
    """
    Insère les instances GCP dans une table dédiée.
    Même schema que raw_aws_instances pour faciliter l'union plus tard.
    """
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_gcp_instances (
            instance_type  VARCHAR PRIMARY KEY,
            family         VARCHAR NOT NULL,
            provider       VARCHAR NOT NULL,
            vcpu           INTEGER NOT NULL,
            memory_gb      FLOAT   NOT NULL,
            os             VARCHAR NOT NULL,
            region         VARCHAR NOT NULL,
            price_ondemand FLOAT   NOT NULL,
            price_reserved FLOAT
        )
    """)

    con.executemany("""
        INSERT OR REPLACE INTO raw_gcp_instances VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, [
        (
            r["instance_type"], r["family"], r["provider"],
            r["vcpu"], r["memory_gb"], r["os"], r["region"],
            r["price_ondemand"], r["price_reserved"]
        )
        for r in rows
    ])

    count = con.execute(
        "SELECT COUNT(*) FROM raw_gcp_instances"
    ).fetchone()[0]

    print(f"\nDone — {count} instances GCP dans DuckDB")
    con.close()


def quick_check() -> None:
    con = duckdb.connect(str(DB_PATH))

    print("\n--- Top 5 instances GCP Linux ≥ 16GB RAM les moins chères ---")
    print(con.execute("""
        SELECT instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4) as price_ondemand
        FROM raw_gcp_instances
        WHERE memory_gb >= 16
        ORDER BY price_ondemand ASC
        LIMIT 5
    """).df().to_string(index=False))

    print("\n--- Comparaison AWS vs GCP pour 16GB RAM ---")
    print(con.execute("""
        SELECT 'aws' as provider, instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4) as price_ondemand
        FROM raw_aws_instances
        WHERE memory_gb >= 16 AND price_ondemand > 0
        UNION ALL
        SELECT 'gcp', instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4)
        FROM raw_gcp_instances
        WHERE memory_gb >= 16
        ORDER BY price_ondemand ASC
        LIMIT 10
    """).df().to_string(index=False))

    con.close()


if __name__ == "__main__":
    if not GCP_API_KEY:
        raise ValueError("GCP_API_KEY manquant — vérifie ton fichier .env")

    skus = fetch_all_skus()
    print(f"\nTotal SKUs fetched : {len(skus)}")

    component_prices = extract_component_prices(skus)
    print(f"\nFamilles trouvées : {list(component_prices.keys())}")
    for fam, prices in component_prices.items():
        print(f"  {fam}: cpu={prices.get('cpu')}, ram={prices.get('ram')}")

    rows = build_instances(component_prices)
    print(f"\nInstances reconstituées : {len(rows)}")

    load_to_duckdb(rows)
    quick_check()