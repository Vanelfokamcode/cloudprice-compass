select
    instance_type,
    family,
    'azure'     as provider,
    vcpu,
    memory_gb,
    os,
    region,
    price_ondemand,
    price_reserved,

    round(price_ondemand / vcpu, 6)       as price_per_vcpu,
    round(price_ondemand / memory_gb, 6)  as price_per_gb_ram,

    case
        when family in ('e', 'm')      then 'memory-optimized'
        when family in ('f', 'h')      then 'compute-optimized'
        when family in ('d', 'a', 'b') then 'general-purpose'
        when family in ('l')           then 'storage-optimized'
        else 'general-purpose'
    end as category

from {{ source('raw', 'raw_azure_instances') }}
where price_ondemand > 0