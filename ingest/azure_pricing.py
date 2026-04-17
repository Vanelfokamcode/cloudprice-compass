import httpx
import re
import duckdb
import json
from pathlib import Path

# --- Config ---
REGION = "westeurope"
DB_PATH = Path("data/compass.duckdb")
CACHE_PATH = Path("data/azure_instances_raw.json")
BASE_URL = "https://prices.azure.com/api/retail/prices"


def fetch_all_items() -> list:
    """
    Fetch toutes les VMs Azure pour notre région.
    L'API est paginée via NextPageLink.

    Pas d'auth requise — c'est l'API publique Azure Retail Prices.
    """
    if CACHE_PATH.exists():
        print(f"Cache trouvé — lecture locale")
        return json.loads(CACHE_PATH.read_bytes())

    all_items = []
    url = BASE_URL
    params = {
        "api-version": "2023-01-01-preview",
        "currencyCode": "USD",
    }
    page = 1

    while url:
        print(f"Fetching page {page}...")
        r = httpx.get(url, params=params if page == 1 else None, timeout=30)
        r.raise_for_status()
        data = r.json()

        items = data.get("Items", [])
        all_items.extend(items)
        print(f"  → {len(items)} items (total: {len(all_items)})")

        # Azure donne l'URL complète de la page suivante
        url = data.get("NextPageLink")
        params = None  # NextPageLink contient déjà les params
        page += 1

        # On arrête quand on a assez — l'API retourne TOUT sans filtre
        # On filtre après, mais on veut pas fetcher 500k lignes
        if len(all_items) > 50000:
            print("Limite atteinte — arrêt pagination")
            break

    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(all_items))
    print(f"Cached {len(all_items)} items")
    return all_items

def parse_specs_from_sku(arm_sku: str) -> dict | None:
    """
    Parse vcpu et memory_gb depuis le nom ARM Azure.
    Couvre ~80% des instances standard.

    Convention : Standard_D4s_v3
    - D = famille general purpose
    - 4 = vcpu count
    - s = premium storage
    - v3 = version

    Ratios RAM/vCPU par famille :
    - D, A, B(ms) → 4 GB/vCPU
    - E, M        → 8 GB/vCPU
    - F           → 2 GB/vCPU
    - L           → 8 GB/vCPU (storage optimized)
    - H           → 8 GB/vCPU (HPC)
    """
    name = arm_sku.replace("Standard_", "")

    # Extrait le nombre de vCPUs — premier nombre dans le nom
    vcpu_match = re.search(r"(\d+)", name)
    if not vcpu_match:
        return None

    vcpu = int(vcpu_match.group(1))
    if vcpu == 0 or vcpu > 512:
        return None

    # Identifie la famille depuis la première lettre
    family_letter = name[0].upper()

    ram_ratios = {
        "D": 4, "A": 4, "B": 4,
        "E": 8, "M": 8, "L": 8, "H": 8,
        "F": 2, "C": 4, "N": 8,
    }

    ratio = ram_ratios.get(family_letter, 4)
    memory_gb = float(vcpu * ratio)

    return {"vcpu": vcpu, "memory_gb": memory_gb}


def extract_instances(items: list) -> list[dict]:
    rows = []
    seen = set()

    for item in items:
        if item.get("armRegionName") != REGION:
            continue
        if item.get("serviceName") != "Virtual Machines":
            continue
        if "Windows" in item.get("productName", ""):
            continue

        sku_name = item.get("skuName", "")
        if any(x in sku_name for x in ["Spot", "Low Priority", "Expired"]):
            continue

        # Fix clé — filtre unitOfMeasure
        if item.get("unitOfMeasure") != "1 Hour":
            continue

        price = item.get("retailPrice", 0)
        if price <= 0 or price > 50:
            continue

        arm_sku = item.get("armSkuName", "")
        if not arm_sku.startswith("Standard_"):
            continue

        if arm_sku in seen:
            continue
        seen.add(arm_sku)

        specs = parse_specs_from_sku(arm_sku)
        if not specs:
            continue

        instance_type = arm_sku.replace("Standard_", "").replace("_", "-").lower()
        family = instance_type[0]

        rows.append({
            "instance_type":  instance_type,
            "family":         family,
            "provider":       "azure",
            "vcpu":           specs["vcpu"],
            "memory_gb":      specs["memory_gb"],
            "os":             "linux",
            "region":         REGION,
            "price_ondemand": round(price, 6),
            "price_reserved": None,
        })

    return rows

    """
    Filtre et normalise les VMs Azure.

    Règles de filtrage :
    - armRegionName == notre région
    - serviceName == "Virtual Machines"
    - productName NE contient PAS "Windows" → Linux only
    - skuName NE contient PAS "Spot" ni "Low Priority" → OnDemand only
    - retailPrice > 0
    - armSkuName commence par "Standard_" → instances standard
    """
    rows = []
    seen = set()  # évite les doublons

    for item in items:
        # Filtre région
        if item.get("armRegionName") != REGION:
            continue

        # Filtre service
        if item.get("serviceName") != "Virtual Machines":
            continue

        # Filtre OS — on veut Linux uniquement
        product_name = item.get("productName", "")
        if "Windows" in product_name:
            continue

        # Filtre unitOfMeasure — on veut UNIQUEMENT les prix horaires
        if item.get("unitOfMeasure") != "1 Hour":
             continue
        # Filtre type — OnDemand uniquement
        sku_name = item.get("skuName", "")
        if any(x in sku_name for x in ["Spot", "Low Priority", "Expired"]):
            continue

        # Filtre prix
        price = item.get("retailPrice", 0)
        if price <= 0:
            continue

        # Filtre nom instance
        arm_sku = item.get("armSkuName", "")
        if not arm_sku.startswith("Standard_"):
            continue

        # Dédoublonnage sur le nom d'instance
        if arm_sku in seen:
            continue
        seen.add(arm_sku)

        # Normalise le nom : "Standard_D4s_v3" → "d4s-v3"
        instance_type = arm_sku.replace("Standard_", "").replace("_", "-").lower()

        # Famille : "Standard_D4s_v3" → "d"
        # Azure naming : D = general purpose, F = compute, E = memory, B = burstable
        family = _extract_family(arm_sku)

        # Azure ne fournit pas vcpu/RAM dans cette API
        # On les ajoute depuis notre table de référence
        specs = AZURE_SPECS.get(arm_sku)
        if not specs:
            continue

        rows.append({
            "instance_type":  instance_type,
            "family":         family,
            "provider":       "azure",
            "vcpu":           specs["vcpu"],
            "memory_gb":      specs["memory_gb"],
            "os":             "linux",
            "region":         REGION,
            "price_ondemand": round(price, 6),
            "price_reserved": None,
        })

    return rows


def _extract_family(arm_sku: str) -> str:
    """
    Extrait la famille depuis le nom ARM Azure.
    "Standard_D4s_v3" → "d"
    "Standard_E8s_v4" → "e"
    "Standard_F4s_v2" → "f"
    """
    name = arm_sku.replace("Standard_", "")
    # Prend les lettres au début jusqu'au premier chiffre
    family = ""
    for char in name.lower():
        if char.isdigit():
            break
        family += char
    return family if family else "unknown"



def load_to_duckdb(rows: list[dict]) -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_azure_instances (
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
        INSERT OR REPLACE INTO raw_azure_instances VALUES (
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
        "SELECT COUNT(*) FROM raw_azure_instances"
    ).fetchone()[0]

    print(f"\nDone — {count} instances Azure dans DuckDB")
    con.close()


def quick_check() -> None:
    con = duckdb.connect(str(DB_PATH))

    print("\n--- Top 5 instances Azure Linux ≥ 16GB RAM les moins chères ---")
    print(con.execute("""
        SELECT instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4) as price_ondemand
        FROM raw_azure_instances
        WHERE memory_gb >= 16
        ORDER BY price_ondemand ASC
        LIMIT 5
    """).df().to_string(index=False))

    print("\n--- Comparaison AWS vs GCP vs Azure pour 16GB RAM ---")
    print(con.execute("""
        SELECT provider, instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4) as price_ondemand
        FROM raw_aws_instances
        WHERE memory_gb >= 16 AND price_ondemand > 0
        UNION ALL
        SELECT provider, instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4)
        FROM raw_gcp_instances
        WHERE memory_gb >= 16
        UNION ALL
        SELECT provider, instance_type, vcpu, memory_gb,
               ROUND(price_ondemand, 4)
        FROM raw_azure_instances
        WHERE memory_gb >= 16
        ORDER BY price_ondemand ASC
        LIMIT 12
    """).df().to_string(index=False))

    con.close()


if __name__ == "__main__":
    items = fetch_all_items()
    print(f"\nTotal items fetched : {len(items)}")

    rows = extract_instances(items)
    print(f"Instances extraites : {len(rows)}")

    load_to_duckdb(rows)
    quick_check()