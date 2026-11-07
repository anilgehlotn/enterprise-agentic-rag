"""Document ingestion: parse → chunk → save locally → embed (dense + BM25) → index in Qdrant.

Used by the CLI (`python -m app.ingestion.processor DATA --wipe`) and the /documents
upload endpoint. Importing this module has no side effects: no Logfire configuration,
no Qdrant client and no embedding model are created until a file is processed.
"""

import json
import os
import sys
import uuid

import logfire
from qdrant_client.http import models

from app.config import settings
from app.ingestion.chunking import ChunkingConfig, chunk_text, resolve_chunking_config
from app.services.retrieval.embedding import embed_texts
from app.services.retrieval.qdrant_service import delete_collection, ensure_collection, get_qdrant_client
from app.services.retrieval.schema import DENSE_VECTOR, SPARSE_VECTOR, CollectionSchemaError
from app.services.retrieval.sparse_embedding import embed_sparse_documents

# Local folder where parsed + chunked JSON metadata is saved (replaces GCS processed bucket)
PROCESSED_DATA_DIR = "processed_data"


def save_processed_locally(data: dict, source_type: str, filename: str) -> str:
    """Save parsed chunk metadata as JSON in processed_data/<source_type>/."""
    folder = os.path.join(PROCESSED_DATA_DIR, source_type)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{filename}.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return dest


def chunk_payload(chunk: str, index: int, filename: str, source_type: str, chunking: ChunkingConfig) -> dict:
    """Qdrant payload of one chunk: text, provenance, and how it was chunked (strategy, size, overlap, index)."""
    return {
        "text": chunk,
        "source": filename,
        "source_type": source_type,
        "chunk_index": index,
        **chunking.as_metadata(),
    }


def build_points(
    chunks: list[str], filename: str, source_type: str, chunking: ChunkingConfig | None = None
) -> list[models.PointStruct]:
    """One point per chunk carrying BOTH named vectors: "dense" (Gemini) and "bm25" (sparse)."""
    chunking = chunking or resolve_chunking_config()
    dense_vectors = embed_texts(chunks)
    sparse_vectors = embed_sparse_documents(chunks)
    if not len(dense_vectors) == len(sparse_vectors) == len(chunks):
        raise RuntimeError(
            f"Embedding count mismatch for {filename}: {len(chunks)} chunks, "
            f"{len(dense_vectors)} dense, {len(sparse_vectors)} sparse."
        )
    return [
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector={DENSE_VECTOR: dense, SPARSE_VECTOR: sparse},
            payload=chunk_payload(chunk, index, filename, source_type, chunking),
        )
        for index, (chunk, dense, sparse) in enumerate(zip(chunks, dense_vectors, sparse_vectors, strict=True))
    ]


def parse_document(file_path: str, filename: str) -> str | None:
    """Text of a PDF/HTML/TXT/DOCX/PPTX file (parser imported on demand); None for other types."""
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        from app.ingestion.loaders.pdf import parse_pdf

        return parse_pdf(file_path)
    if ext in ("html", "htm"):
        from app.ingestion.loaders.html import parse_html

        return parse_html(file_path)
    if ext == "txt":
        from app.ingestion.loaders.text import parse_text

        return parse_text(file_path)
    if ext in ("docx", "pptx"):
        from app.ingestion.loaders.office import parse_office

        return parse_office(file_path)
    return None


def process_file(file_path: str, filename: str, source_type: str):
    """Parse → chunk → save locally → embed → index in Qdrant."""
    with logfire.span("Processing File", file=filename, source=source_type):
        try:
            # 1. Extract text based on file extension (parsers imported on demand)
            full_text = parse_document(file_path, filename)
            if full_text is None:
                logfire.warning(f"Skipping unsupported file type: {filename}")
                return

            if not full_text or not full_text.strip():
                logfire.warning(f"No text extracted from {filename} — skipping.")
                return

            # 2. Chunk text (CHUNKING_STRATEGY / CHUNK_SIZE / CHUNK_OVERLAP)
            chunking = resolve_chunking_config()
            chunks = chunk_text(full_text, chunking)
            if not chunks:
                return

            # 3. Save processed metadata locally. "chunks" stays a list of strings (chunk_index =
            # position in the list), so list_indexed_documents and older snapshots keep working.
            processed_data = {
                "filename": filename,
                "source_type": source_type,
                "chunking": chunking.as_metadata(),
                "chunks": chunks,
            }
            local_path = save_processed_locally(processed_data, source_type, filename)
            logfire.info(f"Saved processed data → {local_path}")

            # 4. Embed (dense + BM25) and index in Qdrant
            with logfire.span("Vectorizing & Indexing"):
                ensure_collection()
                points = build_points(chunks, filename, source_type, chunking)
                get_qdrant_client().upsert(
                    collection_name=settings.QDRANT_COLLECTION,
                    points=points,
                )
                logfire.info(f"Indexed {len(points)} points to Qdrant from {filename}.")
            return {"filename": filename, "chunks_indexed": len(chunks), "processed_path": local_path}

        except CollectionSchemaError as e:
            logfire.error(f"Failed to index {filename}: {e}")
            return {"filename": filename, "error": "old_collection_schema", "detail": str(e)}
        except Exception as e:
            logfire.error(f"Failed to process {filename}: {e}")
            return {"filename": filename, "error": "processing_failed"}


def process_directory(dir_path: str, source_type: str):
    """Process every file in a directory."""
    with logfire.span("Scanning Directory", path=dir_path, source=source_type):
        files = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]
        logfire.info(f"Found {len(files)} files in {dir_path}.")
        for filename in files:
            process_file(os.path.join(dir_path, filename), filename, source_type)


def run_universal_ingestion(base_dir: str, explicit_source_type: str = None, wipe: bool = False):
    """
    Scan base_dir, map sub-folders to source types, and ingest all documents.
    Pass --wipe to drop and recreate the Qdrant collection before ingestion.
    """
    # Invalid CHUNKING_STRATEGY / CHUNK_SIZE / CHUNK_OVERLAP raise ValueError here, before --wipe.
    chunking = resolve_chunking_config()
    with logfire.span("Universal Ingestion Started", base_directory=base_dir, chunking=chunking.label):
        # Wipe collection if requested
        if wipe:
            with logfire.span("Wiping Collection"):
                if delete_collection():
                    logfire.info(f"Collection '{settings.QDRANT_COLLECTION}' deleted.")

        # Create the collection (named dense + bm25 vectors), or fail fast on the old schema.
        ensure_collection()

        # Route to sub-folders or treat the whole dir as one source
        subdirs = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]

        if not subdirs:
            if explicit_source_type:
                source_type = explicit_source_type
            else:
                base_name = os.path.basename(os.path.normpath(base_dir)).lower()
                source_type = "true" if "true" in base_name else "noisy" if "noisy" in base_name else "general"
            logfire.info(f"No sub-folders found — processing '{base_dir}' as '{source_type}'.")
            process_directory(base_dir, source_type)
        else:
            for subdir in subdirs:
                source_type = "true" if "true" in subdir.lower() else "noisy" if "noisy" in subdir.lower() else subdir
                process_directory(os.path.join(base_dir, subdir), source_type)


if __name__ == "__main__":
    logfire.configure(service_name="enterprise-ingestion-service")
    # Usage:
    #   python -m app.ingestion.processor DATA --wipe
    #   python -m app.ingestion.processor DATA/true_data true
    wipe_requested = "--wipe" in sys.argv
    clean_args = [a for a in sys.argv if a != "--wipe"]

    target_dir = clean_args[1] if len(clean_args) > 1 else "DATA"
    explicit_type = clean_args[2] if len(clean_args) > 2 else None

    if not os.path.exists(target_dir):
        print(f"Error: path '{target_dir}' does not exist.")
        sys.exit(1)

    try:
        run_universal_ingestion(target_dir, explicit_source_type=explicit_type, wipe=wipe_requested)
    except (CollectionSchemaError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    logfire.info("Ingestion job completed.")
