select
    instance_type,
    family,
    'gcp'       as provider,
    vcpu,
    memory_gb,
    os,
    region,
    price_ondemand,
    price_reserved,

    round(price_ondemand / vcpu, 6)       as price_per_vcpu,
    round(price_ondemand / memory_gb, 6)  as price_per_gb_ram,

    case
        when family in ('e2', 't2d', 't2a') then 'general-purpose'
        when family in ('c3', 'c3d', 'c4') then 'compute-optimized'
        when family in ('n2', 'n2d', 'n4') then 'general-purpose'
        when family in ('m1', 'm2', 'm3')  then 'memory-optimized'
        else 'general-purpose'
    end as category

from {{ source('raw', 'raw_gcp_instances') }}
where price_ondemand > 0