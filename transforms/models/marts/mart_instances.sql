with aws as (
    select * from {{ ref('stg_aws_instances') }}
),

gcp as (
    select * from {{ ref('stg_gcp_instances') }}
),

azure as (
    select * from {{ ref('stg_azure_instances') }}
),

unioned as (
    select * from aws
    union all
    select * from gcp
    union all
    select * from azure
)

select
    -- Clé unique cross-provider
    provider || ':' || instance_type   as instance_id,

    provider,
    instance_type,
    family,
    category,
    os,
    region,
    vcpu,
    memory_gb,

    -- Prix
    price_ondemand,
    price_reserved,
    price_per_vcpu,
    price_per_gb_ram,

    -- Savings reserved vs ondemand
    case
        when price_reserved is not null and price_reserved > 0
        then round((1 - price_reserved / price_ondemand) * 100, 1)
        else null
    end as reserved_savings_pct,

    -- Score value : plus c'est bas, meilleur le rapport qualité/prix
    -- Combinaison normalisée price/vcpu + price/ram
    round(
        (price_per_vcpu * 0.5) + (price_per_gb_ram * 0.5),
        8
    ) as value_score

from unioned