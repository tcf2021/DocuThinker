import json
from urllib.parse import quote

from . import db
from .config import INDEX_VERSION
from .field_schema import field_catalog


def ready_documents(owner, kb):
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT docs.* FROM docs JOIN kbs ON docs.kb=kbs.id WHERE docs.kb=? AND kbs.owner=? AND docs.status='ready'",
                (kb, owner),
            )
        ]


class KnowledgeBaseRetriever:
    def __init__(self, sv):
        self.sv = sv

    def search(self, owner, kb, question, documents, plan):
        ready = {d["id"]: d for d in documents}
        current = [d for d in documents if d["index_version"] == INDEX_VERSION]
        if not ready:
            return [], []
        vectors = {}
        by_id, trace = {}, []
        explicit = plan["explicit_filters"]
        fields = plan["field_schema"] if "field_schema" in plan else field_catalog(documents)

        def route(name, inferred, docs, index_version=INDEX_VERSION, query=None):
            query = query or question
            if query not in vectors:
                vectors[query] = self.sv.embed.embed_query(query)
            candidates = []
            expressions = []
            for off in range(0, len(docs), 128):
                rows, expression = self.sv.vectors.search(
                    owner,
                    kb,
                    docs[off : off + 128],
                    vectors[query],
                    16,
                    explicit=explicit,
                    inferred=inferred,
                    index_version=index_version,
                    fields=fields,
                )
                expressions.append(expression)
                candidates.extend(rows)
            candidates = sorted(candidates, key=lambda r: r["distance"], reverse=True)[
                :16
            ]
            for r in candidates:
                key = f"v{index_version}:" + str(r["id"])
                if key not in by_id:
                    by_id[key] = r | {
                        "routes": [],
                        "route_queries": {},
                        "index_version": index_version,
                    }
                by_id[key]["routes"].append(name)
                by_id[key]["route_queries"][name] = query
            trace.append(
                {
                    "route": name,
                    "filters": (explicit | inferred)
                    | (
                        {
                            "attributes": explicit.get("attributes", {})
                            | inferred.get("attributes", {})
                        }
                        if explicit.get("attributes") or inferred.get("attributes")
                        else {}
                    ),
                    "candidate_count": len(candidates),
                    "query": query,
                    "expressions": expressions,
                    "index_version": index_version,
                }
            )
            return len(candidates)

        for branch in plan["routes"]:
            route(branch["name"], branch["inferred"], current, query=branch.get("query"))
        rows = list(by_id.values())
        for row in rows:
            doc = ready[row["doc"]]
            base = f"/api/documents/{row['doc']}/files/"
            page = row["page"]
            row.update(
                filename=doc["filename"],
                source_url=base + "source.pdf" + (f"#page={page}" if page else ""),
                image_urls=[
                    base + quote(name)
                    for name in json.loads(doc["manifest"])
                    .get("pages", {})
                    .get(str(page), [])
                ],
            )
        return rows, trace


def rank_routes(sv, question, rows):
    """Rerank supporting rules against their semantic query, not an absent batch ID."""
    groups = {}
    for row in rows:
        for route, query in row.get("route_queries", {"匹配": question}).items():
            groups.setdefault((route, query), []).append(row)
    scored = {}
    for (route, query), candidates in groups.items():
        for row in sv.rank(query, candidates):
            key = row["id"]
            if key not in scored:
                scored[key] = row | {"route_scores": {}, "rerank_query": query}
            scored[key]["route_scores"][route] = row["rerank"]
            if row["rerank"] > scored[key]["rerank"]:
                scored[key]["rerank"] = row["rerank"]
                scored[key]["rerank_query"] = query
    return sorted(scored.values(), key=lambda r: r["rerank"], reverse=True)


def select_evidence(ranked, plan, limit=8):
    """Protect a candidate per route and avoid one document occupying every evidence slot."""
    chosen, keys = [], set()

    def add(row):
        if row["id"] not in keys and len(chosen) < limit:
            chosen.append(row)
            keys.add(row["id"])

    for route in plan["routes"]:
        candidates = [r for r in ranked if route["name"] in r.get("routes", [])]
        if candidates:
            add(
                max(
                    candidates,
                    key=lambda r: r.get("route_scores", {}).get(
                        route["name"], r["rerank"]
                    ),
                )
            )
    for row in ranked:
        if sum(r["doc"] == row["doc"] for r in chosen) < 2:
            add(row)
    for row in ranked:
        add(row)
    return chosen
