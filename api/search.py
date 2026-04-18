import duckdb
from pathlib import Path
from dataclasses import dataclass

DB_PATH = Path("data/compass.duckdb")

# Poids par use case — CPU weight, RAM weight
USE_CASE_WEIGHTS = {
    "general":   (0.5, 0.5),
    "postgres":  (0.2, 0.8),
    "redis":     (0.1, 0.9),
    "web":       (0.7, 0.3),
    "ml":        (0.6, 0.4),
    "spark":     (0.5, 0.5),
}


@dataclass
class SearchQuery:
    """
    Représente une requête de recherche utilisateur.
    Tous les champs sont optionnels sauf ram_min.
    """
    ram_min:      float        = 0.0
    ram_max:      float | None = None
    vcpu_min:     int          = 0
    vcpu_max:     int | None   = None
    price_max:    float | None = None
    providers:    list[str]    = None
    category:     str | None   = None
    use_case:     str          = "general"
    limit:        int          = 20

    def __post_init__(self):
        if self.providers is None:
            self.providers = ["aws", "gcp", "azure"]
        if self.use_case not in USE_CASE_WEIGHTS:
            self.use_case = "general"


def search(q: SearchQuery) -> list[dict]:
    """
    Requête principale du comparateur.

    Logique :
    1. Filtre les instances selon les contraintes dures (RAM, CPU, prix, provider)
    2. Calcule un value_score dynamique selon le use_case
    3. Trie par value_score ASC (plus bas = meilleur rapport qualité/prix)
    4. Retourne les top N résultats
    """
    cpu_w, ram_w = USE_CASE_WEIGHTS[q.use_case]

    # Construction dynamique des filtres WHERE
    filters = ["price_ondemand > 0"]
    params = []

    filters.append("memory_gb >= ?")
    params.append(q.ram_min)

    if q.ram_max:
        filters.append("memory_gb <= ?")
        params.append(q.ram_max)

    if q.vcpu_min > 0:
        filters.append("vcpu >= ?")
        params.append(q.vcpu_min)

    if q.vcpu_max:
        filters.append("vcpu <= ?")
        params.append(q.vcpu_max)

    if q.price_max:
        filters.append("price_ondemand <= ?")
        params.append(q.price_max)

    if q.category:
        filters.append("category = ?")
        params.append(q.category)

    # Filtre providers — IN clause avec placeholders
    provider_placeholders = ", ".join(["?" for _ in q.providers])
    filters.append(f"provider IN ({provider_placeholders})")
    params.extend(q.providers)

    where_clause = " AND ".join(filters)

    sql = f"""
        SELECT
            instance_id,
            provider,
            instance_type,
            family,
            category,
            vcpu,
            memory_gb,
            region,
            price_ondemand,
            price_reserved,
            reserved_savings_pct,

            -- Score dynamique selon use_case
            ROUND(
                (price_per_vcpu * {cpu_w}) + (price_per_gb_ram * {ram_w}),
                8
            ) as value_score,

            -- Coût mensuel estimé (730h = moyenne mois)
            ROUND(price_ondemand * 730, 2) as monthly_cost_usd

        FROM mart_instances
        WHERE {where_clause}
        ORDER BY value_score ASC
        LIMIT {q.limit}
    """

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(sql, params).fetchall()
        cols = [
            "instance_id", "provider", "instance_type", "family",
            "category", "vcpu", "memory_gb", "region",
            "price_ondemand", "price_reserved", "reserved_savings_pct",
            "value_score", "monthly_cost_usd"
        ]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        con.close()


def compare(instance_ids: list[str]) -> list[dict]:
    """
    Retourne les détails complets de plusieurs instances pour comparaison directe.
    Utilisé quand l'utilisateur veut comparer deux instances spécifiques.
    """
    if not instance_ids or len(instance_ids) > 10:
        return []

    placeholders = ", ".join(["?" for _ in instance_ids])

    sql = f"""
        SELECT
            instance_id,
            provider,
            instance_type,
            family,
            category,
            vcpu,
            memory_gb,
            region,
            price_ondemand,
            price_reserved,
            reserved_savings_pct,
            price_per_vcpu,
            price_per_gb_ram,
            ROUND(price_ondemand * 730, 2) as monthly_cost_usd,
            ROUND(price_ondemand * 8760, 2) as yearly_cost_usd
        FROM mart_instances
        WHERE instance_id IN ({placeholders})
        ORDER BY price_ondemand ASC
    """

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(sql, instance_ids).fetchall()
        cols = [
            "instance_id", "provider", "instance_type", "family",
            "category", "vcpu", "memory_gb", "region",
            "price_ondemand", "price_reserved", "reserved_savings_pct",
            "price_per_vcpu", "price_per_gb_ram",
            "monthly_cost_usd", "yearly_cost_usd"
        ]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        con.close()


def get_stats() -> dict:
    """
    Statistiques globales sur le dataset.
    Utilisé par le frontend pour afficher le contexte.
    """
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM mart_instances"
        ).fetchone()[0]

        by_provider = con.execute("""
            SELECT provider, COUNT(*) as nb
            FROM mart_instances
            GROUP BY provider
        """).fetchall()

        last_updated = con.execute("""
            SELECT MAX(effectiveStartDate)
            FROM raw_azure_instances
        """) if False else None  # placeholder

        return {
            "total_instances": total,
            "by_provider": {row[0]: row[1] for row in by_provider},
        }
    finally:
        con.close()