"""Build a revision-scoped field registry from the metadata actually stored in Milvus."""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from copy import deepcopy

from . import db
from .config import INDEX_VERSION
from .field_schema import field_catalog


class MetadataCatalog:
    def __init__(self, vectors, ttl=60, max_entries=64):
        self.vectors = vectors
        self.ttl = max(0, min(float(ttl), 300))
        self.max_entries = max_entries
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def snapshot(self, owner, kb, documents, refresh=False):
        # Recheck authorization even on cache hits. Identity never comes from the model.
        db.retrieval_context(owner, kb)
        fingerprint = hashlib.sha256(
            json.dumps(
                sorted(
                    (
                        d["id"],
                        d["index_version"],
                        d["index_revision"],
                        d["metadata_provenance"],
                        d["metadata_json"],
                    )
                    for d in documents
                ),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        key = (owner, kb, fingerprint)
        with self.lock:
            cached = self.cache.get(key)
            if cached and not refresh and time.monotonic() - cached[0] < self.ttl:
                self.cache.move_to_end(key)
                return deepcopy(cached[1]) | {"cache_hit": True}
            current = [d for d in documents if d["index_version"] == INDEX_VERSION]
            records, schema = self.vectors.metadata_documents(owner, kb, current)
            fields = field_catalog(records)
            snapshot = {
                "filterable_fields": fields,
                "source": "milvus",
                "fingerprint": fingerprint,
                "cache_hit": False,
                "cache_ttl_seconds": self.ttl,
                "indexed_documents": len(records),
                "legacy_documents": len(documents) - len(current),
                "collection_schema": schema,
            }
            # Keep only the newest generation for each user/knowledge base.
            for old in list(self.cache):
                if old[:2] == key[:2]:
                    del self.cache[old]
            self.cache[key] = (time.monotonic(), deepcopy(snapshot))
            while len(self.cache) > self.max_entries:
                self.cache.popitem(last=False)
            return snapshot


def metadata_snapshot(sv, owner, kb, documents, refresh=False):
    # Services owns one cache. Test/service adapters may not implement lazy properties.
    registry = getattr(sv, "_metadata_catalog", None)
    if registry is None:
        registry = MetadataCatalog(sv.vectors, sv.cfg.get("metadata_cache_ttl", 60))
        sv._metadata_catalog = registry
    return registry.snapshot(owner, kb, documents, refresh=refresh)
