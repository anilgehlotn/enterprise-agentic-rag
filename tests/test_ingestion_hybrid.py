"""Ingestion writes a dense AND a BM25 vector per chunk (embedders and Qdrant mocked)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from qdrant_client.http import models

from app.ingestion import processor
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


def test_build_points_carries_both_named_vectors_and_payload(embedders):
    points = processor.build_points(["chunk one", "chunk two"], "jobs.html", "true")

    assert len(points) == 2
    first = points[0]
    assert set(first.vector) == {"dense", "bm25"}
    assert first.vector["dense"] == [0.0, 0.5, 0.25]
    assert first.vector["bm25"] == models.SparseVector(indices=[0, 100], values=[1.0, 0.5])
    assert first.payload == {"text": "chunk one", "source": "jobs.html", "source_type": "true"}
    assert points[1].vector["bm25"].indices == [1, 101]
    assert first.id != points[1].id
    assert embedders["dense"] == embedders["sparse"] == [["chunk one", "chunk two"]]


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


def test_importing_processor_has_no_side_effects():
    """Fresh interpreter: no logfire.configure(), no Qdrant client, no embedding model at import."""
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
from contextlib import nullcontext
from types import SimpleNamespace
configured = []
sys.modules["logfire"] = SimpleNamespace(configure=lambda *a, **k: configured.append(1),
    span=lambda *a, **k: nullcontext(), info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None)
import app.ingestion.processor as p
from app.services.retrieval import embedding, qdrant_service, sparse_embedding
print(configured, qdrant_service.get_qdrant_client.cache_info().currsize,
      sparse_embedding.get_sparse_model.cache_info().currsize, embedding._active_model,
      "fastembed" in sys.modules, "langchain_google_genai" in sys.modules)
"""
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[] 0 0 None False False"
