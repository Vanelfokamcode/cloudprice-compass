from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from api.search import search, compare, get_stats, SearchQuery

app = FastAPI(
    title="CloudPrice Compass API",
    description="Comparateur multi-cloud AWS / GCP / Azure",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/stats")
def stats():
    return get_stats()


@app.get("/search")
def search_instances(
    ram_min:   float = Query(0,    description="RAM minimum en GB"),
    ram_max:   float = Query(None, description="RAM maximum en GB"),
    vcpu_min:  int   = Query(0,    description="vCPU minimum"),
    vcpu_max:  int   = Query(None, description="vCPU maximum"),
    price_max: float = Query(None, description="Prix max $/heure"),
    providers: str   = Query("aws,gcp,azure", description="Providers séparés par virgule"),
    category:  str   = Query(None, description="general-purpose, memory-optimized, compute-optimized"),
    use_case:  str   = Query("general", description="general, postgres, redis, web, ml, spark"),
    limit:     int   = Query(20,   description="Nombre de résultats", le=100),
):
    q = SearchQuery(
        ram_min=ram_min,
        ram_max=ram_max,
        vcpu_min=vcpu_min,
        vcpu_max=vcpu_max,
        price_max=price_max,
        providers=providers.split(","),
        category=category,
        use_case=use_case,
        limit=limit,
    )
    return {"query": q.__dict__, "results": search(q)}


@app.get("/compare")
def compare_instances(
    ids: str = Query(..., description="instance_ids séparés par virgule. Ex: aws:r6g.large,gcp:n2-standard-2")
):
    instance_ids = [i.strip() for i in ids.split(",")]
    return {"results": compare(instance_ids)}