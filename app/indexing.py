"""Build isolated revisions, then atomically publish a SQLite pointer after Milvus flush."""

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import db
from .config import EMBED_MODEL, INDEX_VERSION, VECTOR_DIM
from .metadata import (
    DocumentMetadata,
    chunk_metadata,
    date_number,
    embedding_text,
)
from .metadata_agent import discover
from .field_schema import utc_stamp
from .parsing import Problem, split_pages


def read_object(store, key):
    response = store.read_stream(key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def cached_chunks(sv, doc):
    base = f"agent-kb/{doc['kb']}/{doc['id']}/run-{doc['attempt']}/"
    manifest = json.loads(doc["manifest"])
    blocks = json.loads(read_object(sv.store, base + "content_list.json"))
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    md = read_object(sv.store, base + "document.md").decode("utf-8")
    return split_pages(
        {
            "md": md,
            "blocks": blocks,
            "images": {
                name: b""
                for name in manifest.get("paths", [])
                if name.startswith("images/")
            },
        }
    )


def write_revision(sv, doc, chunks, proposal, revision):
    meta = DocumentMetadata.model_validate(proposal["metadata"]).model_dump()
    with db.connection() as con:
        owner = con.execute(
            "SELECT owner FROM kbs WHERE id=?", (doc["kb"],)
        ).fetchone()[0]
    index = sv.vectors
    # Only clear this uncommitted revision on retry. Old active vectors are retained.
    index.clear_revision(doc["id"], revision)
    for off in range(0, len(chunks), 24):
        batch = chunks[off : off + 24]
        metas = [chunk_metadata(meta, c, doc, revision) for c in batch]
        vectors = sv.embed.embed_documents(
            [embedding_text(c["text"], m) for c, m in zip(batch, metas, strict=True)]
        )
        rows = []
        for i, (chunk, cm, vector) in enumerate(
            zip(batch, metas, vectors, strict=True)
        ):
            if len(vector) != VECTOR_DIM:
                raise Problem("Embedding 维度不符，旧索引未切换。")
            cm["embedding_model"] = EMBED_MODEL
            cm["source_file"] = doc["filename"]
            cm["ingested_at"] = utc_stamp(doc["created"])
            cm["field_definitions"] = proposal.get("field_definitions", {})
            rows.append(
                {
                    "id": f"{doc['id']}_{revision}_{off + i + 1}",
                    "owner": owner,
                    "kb": doc["kb"],
                    "doc": doc["id"],
                    "revision": revision,
                    "text": chunk["text"],
                    "page": chunk["page"],
                    "vec": vector,
                    "metadata": cm,
                    "attributes": meta["attributes"],
                    "doc_type": meta["doc_type"],
                    "version": meta["version"],
                    "organization": meta["organization"],
                    "effective_from": date_number(meta["effective_from"]),
                    "effective_to": date_number(meta["effective_to"]),
                }
            )
        index.add(rows)
    index.finish()
    if index.count_revision(doc["id"], revision) != len(chunks):
        raise Problem("新索引片段数不符，旧索引未切换。")


def activate(con, doc, proposal, revision, n_chunks):
    if doc["status"] == "ready" and doc["n_chunks"]:
        db.remember_index(con, doc)
    row = con.execute(
        "UPDATE docs SET index_version=?,index_revision=?,metadata_json=?,metadata_provenance=?,n_chunks=? "
        "WHERE id=? AND index_revision=?",
        (
            INDEX_VERSION,
            revision,
            json.dumps(proposal["metadata"], ensure_ascii=False),
            json.dumps(proposal, ensure_ascii=False),
            n_chunks,
            doc["id"],
            doc["index_revision"],
        ),
    )
    if not row.rowcount:
        raise Problem("索引版本已改变，本次结果未发布，请重新索引。")


class ReindexState(TypedDict, total=False):
    job: dict
    doc: dict
    chunks: list
    proposal: dict


def reindex_graph(sv):
    def load(s):
        with db.connection() as con:
            doc = dict(
                con.execute(
                    "SELECT * FROM docs WHERE id=?", (s["job"]["doc"],)
                ).fetchone()
            )
        if doc["status"] != "ready":
            raise Problem("文档尚未就绪。")
        return {"doc": doc, "chunks": cached_chunks(sv, doc)}

    def metadata(s):
        stored = s["job"]["metadata_json"]
        previous = json.loads(s["doc"]["metadata_provenance"])
        if stored is not None:
            data = json.loads(stored)
            meta = DocumentMetadata.model_validate(
                data.get("metadata", data)
            ).model_dump()
            proposal = {
                "metadata": meta,
                "method": "manual",
                "provenance": {k: [{"source": "manual"}] for k in meta},
                "schema_version": INDEX_VERSION,
                "agent_version": previous.get("agent_version", 4),
                "field_definitions": previous.get("field_definitions", {})
                | data.get("field_definitions", {}),
            }
        elif previous.get("agent_version") == 4:
            # Re-embedding alone preserves reviewed metadata.
            proposal = json.loads(s["doc"]["metadata_provenance"])
        else:
            proposal = discover(sv, s["doc"], s["chunks"])
        return {"proposal": proposal}

    def embed(s):
        write_revision(sv, s["doc"], s["chunks"], s["proposal"], s["job"]["id"])
        return {}

    def commit(s):
        with db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            activate(con, s["doc"], s["proposal"], s["job"]["id"], len(s["chunks"]))
            con.execute(
                "UPDATE index_jobs SET status='ready',stage='ready' WHERE id=?",
                (s["job"]["id"],),
            )
            db.event(
                con,
                s["doc"]["id"],
                s["doc"]["attempt"],
                "reindexed",
                "通用 Metadata v3 索引已发布",
            )
        return {}

    graph = StateGraph(ReindexState)
    last = START
    for name, fn in [
        ("load", load),
        ("metadata", metadata),
        ("embed_index", embed),
        ("publish", commit),
    ]:

        def run(state, stage=name, callback=fn):
            with db.connection() as con:
                con.execute(
                    "UPDATE index_jobs SET stage=? WHERE id=?",
                    (stage, state["job"]["id"]),
                )
            return callback(state)

        graph.add_node(name, run)
        graph.add_edge(last, name)
        last = name
    graph.add_edge(last, END)
    return graph.compile()
