import httpx
import duckdb
import json
from pathlib import Path

# --- Config ---
REGION = "eu-west-1"
DB_PATH = Path("data/compass.duckdb")
CACHE_PATH = Path("data/aws_instances_raw.json")
SOURCE_URL = "https://instances.vantage.sh/instances.json"


def fetch_raw() -> list:
    """
    Télécharge le JSON depuis ec2instances.info (maintenu par Vantage).
    Si le cache local existe, on l'utilise directement.
    """
    if CACHE_PATH.exists():
        print(f"Cache trouvé — lecture locale ({CACHE_PATH.stat().st_size / 1_000:.0f} KB)")
        return json.loads(CACHE_PATH.read_bytes())

    print("Téléchargement ec2instances.info...")
    r = httpx.get(SOURCE_URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_bytes(r.content)
    print(f"Téléchargé et mis en cache ({len(r.content) / 1_000:.0f} KB)")
    return r.json()


def extract_instances(data: list) -> list[dict]:
    """
    Parse le JSON ec2instances.info et retourne les lignes pour DuckDB.

    Structure du JSON source :
    [
      {
        "instance_type": "t3.medium",
        "vCPU": 2,
        "memory": 4.0,
        "pricing": {
          "eu-west-1": {
            "linux": {
              "ondemand": "0.0464",
              "reserved": {
                "yrTerm1Standard.allUpfront": "0.0320"
              }
            }
          }
        }
      }
    ]
    """
    rows = []

    for instance in data:
        instance_type = instance.get("instance_type")
        vcpu = instance.get("vCPU")
        memory_gb = instance.get("memory")

        if not all([instance_type, vcpu, memory_gb]):
            continue

        # On va chercher le prix pour notre région cible
        region_pricing = instance.get("pricing", {}).get(REGION, {})
        linux_pricing = region_pricing.get("linux", {})

        # Prix on-demand
        ondemand_raw = linux_pricing.get("ondemand")
        if not ondemand_raw:
            continue  # pas de prix pour cette région = on skip

        try:
            price_ondemand = float(ondemand_raw)
        except ValueError:
            continue

        # Prix reserved 1 an all-upfront (optionnel — peut ne pas exister)
        reserved_raw = (
            linux_pricing
            .get("reserved", {})
            .get("yrTerm1Standard.allUpfront")
        )
        price_reserved = float(reserved_raw) if reserved_raw else None

        # Famille d'instance : "t3.medium" → "t3"
        family = instance_type.split(".")[0]

        rows.append({
            "instance_type": instance_type,
            "family":        family,
            "provider":      "aws",
            "vcpu":          int(vcpu),
            "memory_gb":     float(memory_gb),
            "os":            "linux",
            "region":        REGION,
            "price_ondemand": price_ondemand,
            "price_reserved": price_reserved,
        })

    return rows


def load_to_duckdb(rows: list[dict]) -> None:
    """
    Crée la table raw_aws_instances et insère les données.
    INSERT OR REPLACE = idempotent : relançable sans dupliquer.
    """
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_aws_instances (
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
        INSERT OR REPLACE INTO raw_aws_instances VALUES (
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
        "SELECT COUNT(*) FROM raw_aws_instances"
    ).fetchone()[0]

    print(f"Done — {count} instances chargées dans DuckDB.")
    con.close()


def quick_check() -> None:
    """
    Vérifie rapidement ce qu'on a chargé.
    Lance quelques requêtes de sanity check.
    """
    con = duckdb.connect(str(DB_PATH))

    print("\n--- Top 5 instances Linux ≥ 16GB RAM les moins chères ---")
    print(con.execute("""
        SELECT instance_type, vcpu, memory_gb, price_ondemand
        FROM raw_aws_instances
        WHERE memory_gb >= 16
        ORDER BY price_ondemand ASC
        LIMIT 5
    """).df().to_string(index=False))

    print("\n--- Familles disponibles ---")
    print(con.execute("""
        SELECT family, COUNT(*) as nb_instances
        FROM raw_aws_instances
        GROUP BY family
        ORDER BY nb_instances DESC
        LIMIT 10
    """).df().to_string(index=False))

    con.close()


if __name__ == "__main__":
    data = fetch_raw()
    print(f"JSON chargé — {len(data)} instances trouvées")
    rows = extract_instances(data)
    print(f"Extraites après filtrage : {len(rows)} instances")
    load_to_duckdb(rows)
    quick_check()