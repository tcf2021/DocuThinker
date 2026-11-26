import io
import json
import socket
from types import SimpleNamespace

import pytest

from app import db, graphs, main
from app.metadata import DocumentMetadata
from app.services import VectorIndex
from app.config import VECTOR_DIM, LEGACY_COLLECTION


class MemoryResponse(io.BytesIO):
    def release_conn(self):
        pass


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.bucket = "test"
        self.client = self

    def prepare(self):
        pass

    def put(self, key, body, mime):
        self.files[key] = body

    def read_stream(self, key):
        return MemoryResponse(self.files[key])

    def fput_object(self, bucket, key, path, content_type=None):
        from pathlib import Path

        self.files[key] = Path(path).read_bytes()


class ConstantEmbedding:
    def embed_query(self, text):
        return [1.0] + [0.0] * (VECTOR_DIM - 1)

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


@pytest.fixture(scope="session")
def milvus(tmp_path_factory):
    from milvus_lite.server import Server

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = tmp_path_factory.mktemp("milvus")
    server = Server(str(root / "vectors.db"), f"127.0.0.1:{port}")
    assert server.init() and server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def env(tmp_path, monkeypatch, milvus):
    for module in (db, main, graphs):
        monkeypatch.setattr(module, "DATA", tmp_path)
    db.init_db()
    index = VectorIndex({"milvus_uri": milvus})
    store = MemoryStore()
    sv = SimpleNamespace(
        cfg={"intent_mode": "manual"},
        vectors=index,
        embed=ConstantEmbedding(),
        store=store,
    )
    sv.rank = lambda question, rows: [r | {"rerank": 2.0} for r in rows]

    def fake_agent(task, payload, **kwargs):
        assert task == "discover_metadata"
        # Deterministic model substitute for migration/state tests, not LLM quality.
        from app.metadata import propose

        batch = payload["chunks"]
        meta = propose(payload["filename"], batch)["metadata"]
        values = {
            key: value if isinstance(value, list) else [value]
            for key, value in meta.items()
            if key != "attributes" and value and value not in ("unknown", "other")
        }
        values.update(meta["attributes"])
        first = batch[0]
        return json.dumps(
            {
                "fields": [
                    {
                        "key": key,
                        "label": key,
                        "description": "测试模型提取的 " + key,
                        "values": items[:12],
                        "evidence": [
                            {
                                "value": value,
                                "chunk_id": first["chunk_id"],
                                "page": first["page"],
                                "quote": first["text"][:600],
                            }
                            for value in items[:12]
                        ],
                    }
                    for key, items in values.items()
                ]
            }
        )

    sv.agent_completion = fake_agent
    owner, other, kb = db.uid(), db.uid(), db.uid()
    with db.connection() as con:
        con.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            (owner, "one", b"salt", b"digest"),
        )
        con.execute(
            "INSERT INTO users VALUES(?,?,?,?)", (other, "two", b"salt", b"digest")
        )
        con.execute("INSERT INTO kbs VALUES(?,?,?)", (kb, owner, "test"))

    def document(
        filename="policy.pdf",
        text="档案保留规范 V2.0\n南站原始科学帧保留 90 天。",
        metadata=None,
        legacy=False,
        doc_kb=None,
    ):
        from app.indexing import activate, write_revision
        from app.metadata import propose

        target_kb = doc_kb or kb
        did = db.uid()
        chunks = [{"text": text, "page": 1, "section": "", "images": []}]
        manifest = {
            "paths": ["source.pdf", "document.md", "content_list.json"],
            "pages": {},
        }
        with db.connection() as con:
            con.execute(
                "INSERT INTO docs(id,kb,filename,sha,created,status,stage,attempt,manifest,n_chunks) VALUES(?,?,?,?,?,'ready','ready',1,?,1)",
                (did, target_kb, filename, did, 1, json.dumps(manifest)),
            )
            d = dict(con.execute("SELECT * FROM docs WHERE id=?", (did,)).fetchone())
        base = f"agent-kb/{target_kb}/{did}/run-1/"
        store.put(base + "document.md", text.encode(), "text/plain")
        store.put(
            base + "content_list.json",
            json.dumps([{"type": "text", "page_idx": 0, "text": text}]).encode(),
            "application/json",
        )
        if legacy:
            from pymilvus import DataType

            c = index.client
            if not c.has_collection(LEGACY_COLLECTION):
                schema = c.create_schema(auto_id=False, enable_dynamic_field=False)
                for field, length in [
                    ("id", 120),
                    ("kb", 32),
                    ("doc", 32),
                    ("text", 8192),
                ]:
                    schema.add_field(
                        field,
                        DataType.VARCHAR,
                        is_primary=field == "id",
                        max_length=length,
                    )
                schema.add_field("page", DataType.INT64)
                schema.add_field("vec", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
                indexes = c.prepare_index_params()
                indexes.add_index(
                    field_name="vec", index_type="FLAT", metric_type="COSINE"
                )
                c.create_collection(
                    LEGACY_COLLECTION, schema=schema, index_params=indexes
                )
            c.upsert(
                LEGACY_COLLECTION,
                [
                    {
                        "id": did + "_1",
                        "kb": target_kb,
                        "doc": did,
                        "text": text,
                        "page": 1,
                        "vec": sv.embed.embed_query(text),
                    }
                ],
            )
            c.flush(LEGACY_COLLECTION)
        else:
            p = propose(filename, chunks)
            if metadata:
                p["metadata"] = DocumentMetadata.model_validate(
                    p["metadata"] | metadata
                ).model_dump()
            revision = db.uid()
            write_revision(sv, d, chunks, p, revision)
            # This fixture starts as ready just to make it searchable; no fake legacy history.
            d["status"] = "running"
            with db.connection() as con:
                activate(con, d, p, revision, len(chunks))
        with db.connection() as con:
            return dict(con.execute("SELECT * FROM docs WHERE id=?", (did,)).fetchone())

    result = SimpleNamespace(
        sv=sv, owner=owner, other=other, kb=kb, document=document, root=tmp_path
    )
    yield result
    index.client.close()
