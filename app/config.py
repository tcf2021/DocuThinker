"""The installer writes this mounted JSON file without exposing credentials in Compose."""

import json
import os
from pathlib import Path

DATA = Path(os.environ.get("AGENT_DATA", "/data"))
SETTINGS = Path(os.environ.get("AGENT_SETTINGS", "/run/agent/settings.json"))
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-base"
LEGACY_COLLECTION = "agent_chunks_bge_small_zh_v15_v1"
PREVIOUS_COLLECTION = "agent_chunks_bge_small_zh_v15_meta_v2"
COLLECTION = "agent_chunks_bge_small_zh_v15_meta_v3"
INDEX_VERSION = 3
COLLECTIONS = {1: LEGACY_COLLECTION, 2: PREVIOUS_COLLECTION, 3: COLLECTION}
VECTOR_DIM = 512
MAX_UPLOAD = 50 * 1024 * 1024


def settings():
    return json.loads(SETTINGS.read_text(encoding="utf-8"))
