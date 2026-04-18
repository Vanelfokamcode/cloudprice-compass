select
    instance_type,
    regexp_extract(family, '^[a-z]+', 0)  as family,
    'aws'       as provider,
    vcpu,
    memory_gb,
    os,
    region,
    price_ondemand,
    price_reserved,

    round(price_ondemand / vcpu, 6)       as price_per_vcpu,
    round(price_ondemand / memory_gb, 6)  as price_per_gb_ram,

    case
        when regexp_extract(family, '^[a-z]+', 0) in ('r', 'x', 'u') then 'memory-optimized'
        when regexp_extract(family, '^[a-z]+', 0) in ('c')            then 'compute-optimized'
        when regexp_extract(family, '^[a-z]+', 0) in ('m', 't', 'a')  then 'general-purpose'
        when regexp_extract(family, '^[a-z]+', 0) in ('i', 'd')       then 'storage-optimized'
        else 'other'
    end as category

from {{ source('raw', 'raw_aws_instances') }}
where price_ondemand > 0