"""Ingestion writes a dense AND a BM25 vector per chunk (embedders and Qdrant mocked)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from qdrant_client.http import models

from app.ingestion import processor
from app.ingestion.chunking.config import ChunkingConfig
from app.services.retrieval.schema import CollectionSchemaError


class RecordingClient:
    def __init__(self):
        self.upserts = []

    def upsert(self, collection_name, points):
        self.upserts.append((collection_name, points))


@pytest.fixture
def embedders(monkeypatch):
    calls = {"dense": [], "sparse": []}

    def embed_texts(chunks):
        calls["dense"].append(list(chunks))
        return [[float(i), 0.5, 0.25] for i in range(len(chunks))]

    def embed_sparse_documents(chunks):
        calls["sparse"].append(list(chunks))
        return [models.SparseVector(indices=[i, 100 + i], values=[1.0, 0.5]) for i in range(len(chunks))]

    monkeypatch.setattr(processor, "embed_texts", embed_texts)
    monkeypatch.setattr(processor, "embed_sparse_documents", embed_sparse_documents)
    return calls


def test_build_points_carries_both_named_vectors_and_payload(embedders, monkeypatch):
    for name in ("CHUNKING_STRATEGY", "CHUNK_SIZE", "CHUNK_OVERLAP"):
        monkeypatch.delenv(name, raising=False)
    points = processor.build_points(["chunk one", "chunk two"], "jobs.html", "true")

    assert len(points) == 2
    first = points[0]
    assert set(first.vector) == {"dense", "bm25"}
    assert first.vector["dense"] == [0.0, 0.5, 0.25]
    assert first.vector["bm25"] == models.SparseVector(indices=[0, 100], values=[1.0, 0.5])
    assert first.payload == {
        "text": "chunk one",
        "source": "jobs.html",
        "source_type": "true",
        "chunk_index": 0,
        "chunking_strategy": "paragraph",
        "chunk_size": 1500,
        "chunk_overlap": 0,
    }
    assert points[1].payload["chunk_index"] == 1
    assert points[1].vector["bm25"].indices == [1, 101]
    assert first.id != points[1].id
    assert embedders["dense"] == embedders["sparse"] == [["chunk one", "chunk two"]]


def test_build_points_records_the_chunking_config_passed_in(embedders):
    config = ChunkingConfig("recursive", 800, 120)

    points = processor.build_points(["a", "b", "c"], "x.txt", "uploaded", config)

    assert [p.payload["chunk_index"] for p in points] == [0, 1, 2]
    assert {p.payload["chunking_strategy"] for p in points} == {"recursive"}
    assert {(p.payload["chunk_size"], p.payload["chunk_overlap"]) for p in points} == {(800, 120)}


def test_process_file_chunks_with_the_env_config_and_stores_it(tmp_path, monkeypatch, embedders):
    client = RecordingClient()
    monkeypatch.setattr(processor, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(processor, "ensure_collection", lambda: None)
    monkeypatch.setattr(processor, "PROCESSED_DATA_DIR", str(tmp_path / "processed"))
    monkeypatch.setenv("CHUNKING_STRATEGY", "fixed")
    monkeypatch.setenv("CHUNK_SIZE", "40")
    monkeypatch.setenv("CHUNK_OVERLAP", "10")
    document = tmp_path / "notes.txt"
    document.write_text("A Job sets backoffLimit to 4. parallelism: 2 runs two Pods at once.", encoding="utf-8")

    result = processor.process_file(str(document), "notes.txt", "uploaded")

    ((_, points),) = client.upserts
    assert result["chunks_indexed"] == len(points) >= 2
    assert all(len(p.payload["text"]) <= 40 for p in points)
    assert [p.payload["chunk_index"] for p in points] == list(range(len(points)))
    assert {p.payload["chunking_strategy"] for p in points} == {"fixed"}
    saved = json.loads((tmp_path / "processed" / "uploaded" / "notes.txt.json").read_text(encoding="utf-8"))
    assert saved["chunking"] == {"chunking_strategy": "fixed", "chunk_size": 40, "chunk_overlap": 10}
    assert saved["chunks"] == [p.payload["text"] for p in points]  # chunk_index = position in this list


def test_cli_ingestion_rejects_an_invalid_chunking_config_before_wiping(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(processor, "delete_collection", lambda: calls.append("delete"))
    monkeypatch.setenv("CHUNKING_STRATEGY", "sentences")

    with pytest.raises(ValueError, match="CHUNKING_STRATEGY"):
        processor.run_universal_ingestion(str(tmp_path), wipe=True)
    assert calls == []


def test_parse_document_returns_none_for_unsupported_types(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("a,b", encoding="utf-8")

    assert processor.parse_document(str(path), "data.csv") is None


def test_build_points_rejects_mismatched_embedding_counts(embedders, monkeypatch):
    monkeypatch.setattr(processor, "embed_sparse_documents", lambda chunks: [])

    with pytest.raises(RuntimeError, match="Embedding count mismatch"):
        processor.build_points(["a", "b"], "x.txt", "true")


def test_process_file_indexes_dense_and_sparse_vectors(tmp_path, monkeypatch, embedders):
    client = RecordingClient()
    ensured = []
    monkeypatch.setattr(processor, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(processor, "ensure_collection", lambda: ensured.append(True))
    monkeypatch.setattr(processor, "PROCESSED_DATA_DIR", str(tmp_path / "processed"))
    document = tmp_path / "notes.txt"
    document.write_text("A Job sets backoffLimit to 4.\n\nparallelism: 2 runs two Pods.", encoding="utf-8")

    result = processor.process_file(str(document), "notes.txt", "uploaded")

    assert result["filename"] == "notes.txt" and result["chunks_indexed"] >= 1
    assert ensured == [True]
    ((collection, points),) = client.upserts
    assert collection == processor.settings.QDRANT_COLLECTION
    assert len(points) == result["chunks_indexed"]
    assert all(set(p.vector) == {"dense", "bm25"} for p in points)
    assert all(p.payload["source_type"] == "uploaded" for p in points)
    assert (tmp_path / "processed" / "uploaded" / "notes.txt.json").is_file()


def test_process_file_reports_old_schema_instead_of_indexing(tmp_path, monkeypatch, embedders):
    client = RecordingClient()

    def old_schema():
        raise CollectionSchemaError("'enterprise_rag' has a single unnamed vector")

    monkeypatch.setattr(processor, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(processor, "ensure_collection", old_schema)
    monkeypatch.setattr(processor, "PROCESSED_DATA_DIR", str(tmp_path / "processed"))
    document = tmp_path / "notes.txt"
    document.write_text("Some text.", encoding="utf-8")

    result = processor.process_file(str(document), "notes.txt", "uploaded")

    assert result["error"] == "old_collection_schema"
    assert "re-ingest with: python -m app.ingestion.processor DATA --wipe" in result["detail"]
    assert client.upserts == []


def test_cli_ingestion_stops_on_old_schema_before_processing(tmp_path, monkeypatch):
    processed = []

    def old_schema():
        raise CollectionSchemaError()

    monkeypatch.setattr(processor, "ensure_collection", old_schema)
    monkeypatch.setattr(processor, "process_directory", lambda *args: processed.append(args))
    (tmp_path / "true_data").mkdir()

    with pytest.raises(CollectionSchemaError, match="re-ingest with"):
        processor.run_universal_ingestion(str(tmp_path))
    assert processed == []


def test_wipe_deletes_then_recreates_through_the_single_create_function(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(processor, "delete_collection", lambda: calls.append("delete") or True)
    monkeypatch.setattr(processor, "ensure_collection", lambda: calls.append("ensure"))
    monkeypatch.setattr(processor, "process_directory", lambda path, source_type: calls.append(source_type))
    (tmp_path / "true_data").mkdir()
    (tmp_path / "noisy_data").mkdir()

    processor.run_universal_ingestion(str(tmp_path), wipe=True)

    assert calls[:2] == ["delete", "ensure"]
    assert sorted(calls[2:]) == ["noisy", "true"]


# Runs in a fresh interpreter. Guards are installed BEFORE app.ingestion.processor is imported;
# each one records the attempt and raises instead of doing the work. Third-party packages may
# import whatever they like (qdrant_client imports fastembed when it is installed); what must
# not happen is a client, a model or a network connection.
_IMPORT_PROBE = """
import json, socket, sys
from contextlib import nullcontext
from types import SimpleNamespace

events = []

def guard(name):
    def blocked(*args, **kwargs):
        events.append(name)
        raise RuntimeError(f"blocked at import time: {name}")
    return blocked

sys.modules["logfire"] = SimpleNamespace(configure=guard("logfire.configure"),
    span=lambda *a, **k: nullcontext(), info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None)

# Network: TCP connects and DNS lookups.
socket.socket.connect = guard("network: socket.connect")
socket.create_connection = guard("network: socket.create_connection")
socket.getaddrinfo = guard("network: socket.getaddrinfo")

# Clients and models, from the libraries themselves (whether or not our factories are used).
import qdrant_client
qdrant_client.QdrantClient.__init__ = guard("QdrantClient()")
fastembed_classes = []
try:
    import fastembed
except ImportError:
    fastembed = None
else:
    for cls_name in ("SparseTextEmbedding", "TextEmbedding"):
        cls = getattr(fastembed, cls_name, None)
        if cls is not None:
            cls.__init__ = guard(f"fastembed.{cls_name}()")
            fastembed_classes.append(cls)

import app.ingestion.processor  # the import under test
from app.services.retrieval import embedding, qdrant_service, sparse_embedding

import_events = list(events)
state = {
    "qdrant_client_cached": qdrant_service.get_qdrant_client.cache_info().currsize,
    "sparse_model_cached": sparse_embedding.get_sparse_model.cache_info().currsize,
    "dense_model_loaded": embedding._active_model is not None,
}

# Prove the guards are live in this environment (otherwise the test would pass vacuously).
events.clear()
probes = [
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: socket.create_connection(("example.com", 443)),
    lambda: socket.socket().connect(("127.0.0.1", 9)),
    lambda: qdrant_client.QdrantClient(":memory:"),
    lambda: sys.modules["logfire"].configure(),
] + [lambda cls=cls: cls() for cls in fastembed_classes]
for probe in probes:
    try:
        probe()
    except RuntimeError:
        pass

print("RESULT " + json.dumps({"import_events": import_events, "state": state,
    "probe_events": list(events), "fastembed_installed": fastembed is not None,
    "fastembed_guards": [f"fastembed.{cls.__name__}()" for cls in fastembed_classes]}))
"""


def test_importing_processor_has_no_side_effects():
    """Fresh interpreter: importing the ingestion module creates no client, loads no model, opens no connection."""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], cwd=root, env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    report = json.loads(line.removeprefix("RESULT "))

    assert report["import_events"] == []
    assert report["state"] == {"qdrant_client_cached": 0, "sparse_model_cached": 0, "dense_model_loaded": False}
    # Every guard fired when exercised on purpose, so an empty import_events list means something.
    expected_probes = [
        "network: socket.getaddrinfo",
        "network: socket.create_connection",
        "network: socket.connect",
        "QdrantClient()",
        "logfire.configure",
    ]
    if report["fastembed_installed"]:
        assert "fastembed.SparseTextEmbedding()" in report["fastembed_guards"]  # the BM25 model class
    assert report["probe_events"] == expected_probes + report["fastembed_guards"]
