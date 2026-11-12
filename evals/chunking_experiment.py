"""Chunking experiment: which chunking strategy / size / overlap retrieves best?

For every configuration of the grid, the corpus (DATA/true_data + DATA/noisy_data,
parsed by the ingestion loaders) is chunked, indexed into a TEMPORARY in-memory Qdrant
(QdrantClient(":memory:"), never the Qdrant Cloud collections) with the production
hybrid layout (named "dense" + "bm25" vectors, IDF modifier, dense + BM25 prefetch fused
with RRF), and the 15 golden questions are answered like the retriever node does:
hybrid top-15 candidates → FlashRank → top 5.

Grid (full):   {paragraph, fixed, recursive, semantic} × chunk size {500, 1000, 1500}
               × overlap {0, 15% of size} for fixed and recursive (paragraph and
               semantic don't overlap) → 18 configurations.
Grid (--quick): sizes {500, 1000}, overlap 15% only for fixed/recursive → 8 configurations.

Embeddings: --embedder local (default) = sentence-transformers all-MiniLM-L6-v2 (free,
no API key, no rate limit); --embedder gemini = the production Gemini model (needs
GEMINI_API_KEY; tens of thousands of calls). Dense vectors are cached per unique text,
so a chunk shared by two configurations and the semantic chunker's sentence vectors
(shared by its three sizes) are embedded once. The semantic chunker uses the same
embedder. BM25 is the production fastembed Qdrant/bm25 model.

Metrics per configuration (evals/retrieval_metrics.py): hit rate@5 and MRR@5 with the
partial-match rule chunk_covers() (fair across chunk sizes), hit rate@5 with the strict
production rule chunk_matches() for reference, coverage@5 (share of the golden
evidence in the top 5), noise rate@5, number of chunks, average chunk length, context
characters in the top 5 (LLM cost proxy), embedded characters (ingestion embedding
cost, independent of the cache) and indexing time (wall clock in this run, lower for
configurations whose texts were already cached).

Each configuration's per-question reciprocal ranks are compared with the best
configuration's by a paired bootstrap (10,000 resamples of the 15 questions): a 95%
interval for ΔMRR that contains 0 means "not distinguishable from the best".

Run (no API keys needed with the local embedder; downloads MiniLM, BM25 and FlashRank
models on first use):

    python -m evals.chunking_experiment
    python -m evals.chunking_experiment --quick
    python -m evals.chunking_experiment --embedder gemini

Writes evals/results/chunking_experiment.md.
"""

import argparse
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.ingestion.chunking import get_chunker
from app.ingestion.chunking.config import FIXED, RECURSIVE, STRATEGIES, ChunkingConfig
from app.ingestion.chunking.semantic import DEFAULT_BREAKPOINT_PERCENTILE, DEFAULT_MIN_CHUNK_FRACTION
from evals.retrieval_benchmark import load_samples
from evals.retrieval_metrics import (
    MIN_SHARED_BIGRAMS,
    PARTIAL_OVERLAP_THRESHOLD,
    chunk_covers,
    chunk_matches,
    context_coverage,
    first_relevant_rank,
    hit_rate,
    mean_reciprocal_rank,
    noise_rate,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals" / "golden_dataset.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "chunking_experiment.md"
DEFAULT_CORPUS = ((ROOT / "DATA" / "true_data", "true"), (ROOT / "DATA" / "noisy_data", "noisy"))

SIZES = (500, 1000, 1500)
QUICK_SIZES = (500, 1000)
OVERLAP_FRACTION = 0.15
OVERLAP_STRATEGIES = (FIXED, RECURSIVE)
CURRENT_DEFAULT = ChunkingConfig()  # paragraph / 1500 / 0

LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION = "chunking_experiment"
UPSERT_BATCH = 256
BOOTSTRAP_SAMPLES = 10_000


# ── Grid and corpus ───────────────────────────────────────────────────────────


def build_grid(quick: bool = False) -> list[ChunkingConfig]:
    """The configurations to compare (see the module docstring)."""
    grid = []
    for size in QUICK_SIZES if quick else SIZES:
        overlap = int(size * OVERLAP_FRACTION)
        for strategy in STRATEGIES:
            if strategy in OVERLAP_STRATEGIES:
                overlaps = (overlap,) if quick else (0, overlap)
            else:
                overlaps = (0,)
            grid.extend(ChunkingConfig(strategy, size, o) for o in overlaps)
    return grid


@dataclass(frozen=True)
class Document:
    filename: str
    source_type: str
    text: str


def load_corpus(
    folders: Sequence[tuple[Path, str]],
    parse: Callable[[str, str], str | None] | None = None,
    log: Callable[[str], None] | None = None,
):
    """
    Parses every supported file in each (folder, source_type) with the ingestion loaders.

    A folder that does not exist is skipped with a note instead of raising: the distractor
    corpus (DATA/noisy_data) is not distributed with the repository (see DATA/README.md).
    """
    if parse is None:
        from app.ingestion.processor import parse_document as parse

    documents = []
    for folder, source_type in folders:
        folder = Path(folder)
        if not folder.is_dir():
            if log:
                log(f"Skipping '{source_type}' corpus: {folder} not found (see DATA/README.md).")
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            text = parse(str(path), path.name)
            if text and text.strip():
                documents.append(Document(path.name, source_type, text))
    return documents


# ── Cached embedders ──────────────────────────────────────────────────────────


class CachedEmbedder:
    """Dense embedder with a per-text cache. requested_chars counts every request (the cold-cache cost)."""

    def __init__(self, name: str, dim: int, embed_documents, embed_query):
        self.name, self.dim = name, dim
        self._embed_documents, self._embed_query = embed_documents, embed_query
        self._cache: dict[str, list[float]] = {}
        self._query_cache: dict[str, list[float]] = {}
        self.requested_chars = 0
        self.computed_texts = 0

    def documents(self, texts: list[str]) -> list[list[float]]:
        self.requested_chars += sum(len(t) for t in texts)
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            vectors = self._embed_documents(missing)
            if len(vectors) != len(missing):
                raise RuntimeError(f"{self.name} returned {len(vectors)} vectors for {len(missing)} texts.")
            self._cache.update(zip(missing, (list(map(float, v)) for v in vectors), strict=True))
            self.computed_texts += len(missing)
        return [self._cache[t] for t in texts]

    def query(self, text: str) -> list[float]:
        if text not in self._query_cache:
            self._query_cache[text] = list(map(float, self._embed_query(text)))
        return self._query_cache[text]


class CachedSparse:
    """BM25 sparse embedder with a per-text cache (documents) — the production fastembed model by default."""

    def __init__(self, embed_documents=None, embed_query=None):
        if embed_documents is None or embed_query is None:
            from app.services.retrieval.sparse_embedding import embed_sparse_documents, embed_sparse_query

            embed_documents, embed_query = embed_sparse_documents, embed_sparse_query
        self._embed_documents, self._embed_query = embed_documents, embed_query
        self._cache: dict = {}

    def documents(self, texts: list[str]) -> list:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            self._cache.update(zip(missing, self._embed_documents(missing), strict=True))
        return [self._cache[t] for t in texts]

    def query(self, text: str):
        return self._embed_query(text)


def local_embedder(model_name: str = LOCAL_MODEL) -> CachedEmbedder:
    """sentence-transformers model on CPU (imported here: it pulls PyTorch, not in requirements-prod/dev)."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)

    def encode(texts):
        return model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False).tolist()

    return CachedEmbedder(
        f"{model_name} (local, max {model.max_seq_length} tokens)",
        model.get_sentence_embedding_dimension(),
        encode,
        lambda text: encode([text])[0],
    )


def gemini_embedder() -> CachedEmbedder:
    """The production Gemini embedder. Refuses to run on the silent sentence-transformers fallback."""
    from app.services.retrieval import embedding

    if embedding.get_embedding_model_name() != embedding.GEMINI_MODEL:
        raise SystemExit("Gemini embeddings unavailable (check GEMINI_API_KEY); use --embedder local.")
    return CachedEmbedder(
        embedding.GEMINI_MODEL, embedding.get_embedding_dim(), embedding.embed_texts, embedding.embed_query
    )


def flashrank_rerank(question: str, records: list[dict], top_n: int) -> list[dict]:
    """FlashRank like the retriever node, but errors raise (ranking_service falls back silently)."""
    from flashrank import RerankRequest

    from app.services.retrieval.ranking_service import _get_ranker

    if not records:
        return []
    passages = [{"id": i, "text": r["content"]} for i, r in enumerate(records)]
    results = _get_ranker().rerank(RerankRequest(query=question, passages=passages))
    return [records[r["id"]] for r in results[:top_n]]


# ── One configuration ─────────────────────────────────────────────────────────


@dataclass
class ConfigResult:
    config: ChunkingConfig
    n_chunks: int = 0
    avg_chunk_len: float = 0.0
    embed_chars: int = 0
    chunk_s: float = 0.0
    index_s: float = 0.0
    ranks: list[int | None] = field(default_factory=list)  # partial-match rule
    strict_ranks: list[int | None] = field(default_factory=list)  # production chunk_matches rule
    coverages: list[float] = field(default_factory=list)
    source_types: list[list[str]] = field(default_factory=list)
    context_chars: list[int] = field(default_factory=list)

    @property
    def reciprocal_ranks(self) -> list[float]:
        return [0.0 if r is None else 1.0 / r for r in self.ranks]

    def summary(self, k: int = 5) -> dict:
        n = len(self.ranks)
        return {
            "hit_rate": hit_rate(self.ranks),
            "mrr": mean_reciprocal_rank(self.ranks),
            "strict_hit_rate": hit_rate(self.strict_ranks),
            "coverage": sum(self.coverages) / n if n else 0.0,
            "noise_rate": noise_rate(self.source_types, k),
            "context_chars": sum(self.context_chars) / n if n else 0.0,
        }


def _chunk_corpus(config: ChunkingConfig, documents: Sequence[Document], embedder: CachedEmbedder):
    chunker = get_chunker(config.strategy, embed_fn=embedder.documents)
    return [
        (document, index, chunk)
        for document in documents
        for index, chunk in enumerate(chunker(document.text, config.chunk_size, config.chunk_overlap))
        if chunk.strip()
    ]


def _index(client, chunks, config: ChunkingConfig, dense: CachedEmbedder, sparse: CachedSparse) -> None:
    from qdrant_client.http import models

    from app.ingestion.processor import chunk_payload
    from app.services.retrieval.qdrant_service import collection_schema
    from app.services.retrieval.schema import DENSE_VECTOR, SPARSE_VECTOR

    client.create_collection(collection_name=COLLECTION, **collection_schema(dense.dim))
    texts = [chunk for _, _, chunk in chunks]
    dense_vectors, sparse_vectors = dense.documents(texts), sparse.documents(texts)
    points = [
        models.PointStruct(
            id=point_id,
            vector={DENSE_VECTOR: dense_vec, SPARSE_VECTOR: sparse_vec},
            payload=chunk_payload(chunk, index, document.filename, document.source_type, config),
        )
        for point_id, ((document, index, chunk), dense_vec, sparse_vec) in enumerate(
            zip(chunks, dense_vectors, sparse_vectors, strict=True)
        )
    ]
    for start in range(0, len(points), UPSERT_BATCH):
        client.upsert(collection_name=COLLECTION, points=points[start : start + UPSERT_BATCH])


def _search(client, question: str, dense: CachedEmbedder, sparse: CachedSparse, limit: int) -> list[dict]:
    from app.services.retrieval.qdrant_service import build_query_request
    from app.services.retrieval.schema import HYBRID

    request = build_query_request(dense.query(question), sparse.query(question), HYBRID, limit)
    response = client.query_points(collection_name=COLLECTION, **request)
    return [
        {
            "content": p.payload.get("text", ""),
            "source": p.payload.get("source"),
            "source_type": p.payload.get("source_type"),
        }
        for p in response.points
    ]


def run_config(
    config: ChunkingConfig,
    documents: Sequence[Document],
    samples: Sequence[dict],
    dense: CachedEmbedder,
    sparse: CachedSparse,
    rerank=flashrank_rerank,
    k: int = 5,
    candidates: int = 15,
) -> ConfigResult:
    """Chunk → index in a fresh in-memory Qdrant → hybrid search + rerank every golden question."""
    from qdrant_client import QdrantClient

    result = ConfigResult(config)
    requested_before = dense.requested_chars
    start = time.perf_counter()
    chunks = _chunk_corpus(config, documents, dense)
    result.chunk_s = time.perf_counter() - start

    result.n_chunks = len(chunks)
    result.avg_chunk_len = sum(len(c) for _, _, c in chunks) / len(chunks) if chunks else 0.0

    client = QdrantClient(":memory:")  # never the Qdrant Cloud collections
    try:
        start = time.perf_counter()
        _index(client, chunks, config, dense, sparse)
        result.index_s = time.perf_counter() - start
        result.embed_chars = dense.requested_chars - requested_before

        for sample in samples:
            question, golden = sample["question"], sample["relevant_contexts"]
            top = rerank(question, _search(client, question, dense, sparse, candidates), k)[:k]
            texts = [r["content"] for r in top]
            result.ranks.append(first_relevant_rank(texts, golden, k, match=chunk_covers))
            result.strict_ranks.append(first_relevant_rank(texts, golden, k, match=chunk_matches))
            result.coverages.append(context_coverage(texts, golden, k))
            result.source_types.append([r.get("source_type") or "unknown" for r in top])
            result.context_chars.append(sum(len(t) for t in texts))
    finally:
        client.close()
    return result


def run_experiment(configs, documents, samples, dense, sparse, rerank=flashrank_rerank, k=5, candidates=15, log=None):
    results = []
    for number, config in enumerate(configs, start=1):
        if log:
            log(f"[{number}/{len(configs)}] {config.label} ...")
        result = run_config(config, documents, samples, dense, sparse, rerank, k, candidates)
        if log:
            s = result.summary(k)
            log(
                f"    {result.n_chunks} chunks, hit {s['hit_rate']:.2f}, MRR {s['mrr']:.3f}, "
                f"{result.chunk_s + result.index_s:.0f}s"
            )
        results.append(result)
    return results


# ── Statistics ────────────────────────────────────────────────────────────────


def paired_bootstrap_ci(a: Sequence[float], b: Sequence[float], samples: int = BOOTSTRAP_SAMPLES, seed: int = 0):
    """95% percentile interval of mean(a - b) over questions resampled with replacement."""
    if len(a) != len(b) or not a:
        raise ValueError("paired_bootstrap_ci needs two non-empty sequences of equal length.")
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    return means[int(0.025 * samples)], means[int(0.975 * samples) - 1]


def rank_results(results: Sequence[ConfigResult], k: int = 5) -> list[ConfigResult]:
    """Sorted by MRR, then hit rate, then fewer context characters (cheaper wins a tie)."""
    return sorted(
        results,
        key=lambda r: (-r.summary(k)["mrr"], -r.summary(k)["hit_rate"], r.summary(k)["context_chars"]),
    )


# ── Report ────────────────────────────────────────────────────────────────────


def _overlap_label(config: ChunkingConfig) -> str:
    if not config.chunk_overlap:
        return "0"
    return f"{config.chunk_overlap} ({round(100 * config.chunk_overlap / config.chunk_size)}%)"


def results_table(results: Sequence[ConfigResult], k: int = 5) -> str:
    ranked = rank_results(results, k)
    best = ranked[0]
    lines = [
        f"| # | Strategy | Size | Overlap | Chunks | Avg len | Hit@{k} | MRR@{k} | ΔMRR vs best (95% CI) "
        f"| Strict hit@{k} | Coverage@{k} | Noise@{k} ↓ | Ctx chars@{k} | Embedded chars | Index s |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for position, r in enumerate(ranked, start=1):
        s = r.summary(k)
        if r is best:
            delta = "best"
        else:
            low, high = paired_bootstrap_ci(r.reciprocal_ranks, best.reciprocal_ranks)
            diff = s["mrr"] - best.summary(k)["mrr"]
            verdict = "within noise" if low <= 0 <= high else "worse"
            delta = f"{diff:+.3f} [{low:+.2f}, {high:+.2f}] {verdict}"
        default = " (current default)" if r.config == CURRENT_DEFAULT else ""
        lines.append(
            f"| {position} | {r.config.strategy}{default} | {r.config.chunk_size} | {_overlap_label(r.config)} "
            f"| {r.n_chunks:,} | {r.avg_chunk_len:.0f} | {s['hit_rate']:.2f} | {s['mrr']:.3f} | {delta} "
            f"| {s['strict_hit_rate']:.2f} | {s['coverage']:.2f} | {s['noise_rate']:.2f} | {s['context_chars']:,.0f} "
            f"| {r.embed_chars / 1e6:.1f}M | {r.chunk_s + r.index_s:.0f} |"
        )
    return "\n".join(lines)


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def group_table(results: Sequence[ConfigResult], key: Callable[[ChunkingConfig], str], title: str, k: int = 5) -> str:
    groups: dict[str, list[ConfigResult]] = {}
    for r in results:
        groups.setdefault(key(r.config), []).append(r)
    lines = [
        f"| {title} | Configs | Mean hit@{k} | Mean MRR@{k} | Mean coverage@{k} | Mean ctx chars@{k} |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, rs in groups.items():
        summaries = [r.summary(k) for r in rs]
        lines.append(
            f"| {name} | {len(rs)} | {_mean(s['hit_rate'] for s in summaries):.2f} "
            f"| {_mean(s['mrr'] for s in summaries):.3f} | {_mean(s['coverage'] for s in summaries):.2f} "
            f"| {_mean(s['context_chars'] for s in summaries):,.0f} |"
        )
    return "\n".join(lines)


def overlap_table(results: Sequence[ConfigResult], k: int = 5) -> str:
    """Same strategy and size, overlap 0 vs 15%: paired effect of overlap."""
    by_key = {(r.config.strategy, r.config.chunk_size, r.config.chunk_overlap > 0): r for r in results}
    lines = [
        f"| Strategy | Size | MRR@{k} 0 → 15% | Hit@{k} 0 → 15% | Coverage@{k} 0 → 15% | Chunks 0 → 15% |",
        "|---|---:|---|---|---|---|",
    ]
    for (strategy, size, has_overlap), with_overlap in by_key.items():
        without = by_key.get((strategy, size, False))
        if not has_overlap or without is None:
            continue
        a, b = without.summary(k), with_overlap.summary(k)
        lines.append(
            f"| {strategy} | {size} | {a['mrr']:.3f} → {b['mrr']:.3f} | {a['hit_rate']:.2f} → {b['hit_rate']:.2f} "
            f"| {a['coverage']:.2f} → {b['coverage']:.2f} | {without.n_chunks:,} → {with_overlap.n_chunks:,} "
            f"(+{100 * (with_overlap.n_chunks / max(without.n_chunks, 1) - 1):.0f}%) |"
        )
    return "\n".join(lines) if len(lines) > 2 else "_No overlap pairs in this grid._"


def per_question_table(results: Sequence[ConfigResult], samples: Sequence[dict], k: int = 5, top: int = 5) -> str:
    ranked = rank_results(results, k)
    shown = ranked[:top] + [r for r in ranked[top:] if r.config == CURRENT_DEFAULT]
    lines = [
        "| # | Question | " + " | ".join(r.config.label for r in shown) + " |",
        "|---|---|" + "---:|" * len(shown),
    ]
    for index, sample in enumerate(samples):
        question = sample["question"] if len(sample["question"]) <= 60 else sample["question"][:57] + "..."
        ranks = " | ".join("–" if r.ranks[index] is None else str(r.ranks[index]) for r in shown)
        lines.append(f"| {sample.get('id', index + 1)} | {question} | {ranks} |")
    return "\n".join(lines)


FINDINGS_PLACEHOLDER = "_Not written yet: read the tables above and write the findings by hand after a run._"


def render_report(results, samples, meta: dict, k: int = 5, candidates: int = 15) -> str:
    n = len(samples)
    return f"""# Chunking experiment

- **Date:** {meta["date"]}
- **Grid:** {meta["grid"]} ({len(results)} configurations)
- **Corpus:** {meta["corpus"]}
- **Questions:** {n} golden questions (`evals/golden_dataset.json`); one question = {100 / n:.1f} points of hit rate
- **Dense embedder:** `{meta["embedder"]}` ({meta["dim"]}-dim), cached per unique text
- **Sparse:** fastembed `Qdrant/bm25`, Qdrant IDF modifier
- **Retrieval:** Qdrant in-memory (`QdrantClient(":memory:")`), dense + BM25 prefetch fused with RRF,
  top {candidates} candidates → FlashRank (`ms-marco-TinyBERT-L-2-v2`) → top {k}: the retriever node's pipeline
- **Semantic chunker:** breakpoint at the {DEFAULT_BREAKPOINT_PERCENTILE:g}th percentile of neighbour-sentence
  cosine similarity (per document), minimum chunk {DEFAULT_MIN_CHUNK_FRACTION:.0%} of the size; sentences
  embedded with the same model
- **Hit rule:** `chunk_covers()` in `evals/retrieval_metrics.py`: containment, or ≥ {MIN_SHARED_BIGRAMS} shared
  word bigrams that are ≥ {PARTIAL_OVERLAP_THRESHOLD:.0%} of the smaller text's bigrams (corpus check in its
  docstring). "Strict hit" uses the production benchmark rule `chunk_matches()`.

> **Embedder caveat.** These numbers were produced with `{meta["embedder"]}`, not the production Gemini
> model, so absolute values will differ with `--embedder gemini`. What we compare is the relative ranking of
> chunking configurations under one fixed retrieval stack.

## Results (sorted by MRR@{k})

{results_table(results, k)}

Column notes: **ΔMRR vs best**: paired bootstrap over the {n} questions; an interval containing 0 means the
configuration can't be told apart from the best on this dataset. **Coverage@{k}**: share of the golden
contexts' word bigrams found in the top {k} (how much of the evidence the LLM receives). **Ctx chars@{k}**:
characters handed to the LLM per question (cost proxy). **Embedded chars**: characters sent to the dense
embedder at ingestion with a cold cache (semantic includes the sentence embeddings). **Index s**: chunk +
embed + upsert wall time in this run; lower when an earlier configuration already embedded the same texts
(notably semantic at 1000 and 1500, whose sentence vectors were cached at 500).

## By strategy

{group_table(results, lambda c: c.strategy, "Strategy", k)}

## By chunk size

{group_table(results, lambda c: str(c.chunk_size), "Chunk size", k)}

## Effect of overlap (same strategy and size)

{overlap_table(results, k)}

## Rank of the first relevant chunk per question

Top configurations and the current default (`–` = no relevant chunk in the top {k}).

{per_question_table(results, samples, k)}

## Findings

{FINDINGS_PLACEHOLDER}

Generated by `python -m evals.chunking_experiment{meta["args"]}`.
"""


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evals.chunking_experiment", description=__doc__.split("\n")[0])
    parser.add_argument("--quick", action="store_true", help="smaller grid (8 configurations)")
    parser.add_argument("--embedder", choices=("local", "gemini"), default="local")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--candidates", type=int, default=15, help="hybrid candidates before FlashRank (default 15)")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(argv=None) -> None:
    args = parse_args(argv)
    import logfire

    logfire.configure(send_to_logfire=False, console=False)

    samples = load_samples(args.dataset)
    _log("Parsing corpus ...")
    documents = load_corpus(DEFAULT_CORPUS, log=_log)
    if not documents:
        folders = ", ".join(str(folder) for folder, _ in DEFAULT_CORPUS)
        _log(f"No documents parsed from {folders}. See DATA/README.md for how to supply a corpus.")
        raise SystemExit(1)
    dense = local_embedder() if args.embedder == "local" else gemini_embedder()
    sparse = CachedSparse()
    configs = build_grid(args.quick)

    results = run_experiment(
        configs, documents, samples, dense, sparse, flashrank_rerank, k=args.k, candidates=args.candidates, log=_log
    )

    by_type = {t: [d for d in documents if d.source_type == t] for t in ("true", "noisy")}
    corpus = ", ".join(
        f"{len(docs)} {t} documents ({sum(len(d.text) for d in docs) / 1e6:.2f}M chars)" for t, docs in by_type.items()
    )
    if not by_type["noisy"]:
        corpus += " — no distractor corpus present, so noise rate is 0 by construction"
    meta = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "grid": "quick" if args.quick else "full",
        "corpus": corpus,
        "embedder": dense.name,
        "dim": dense.dim,
        "args": "".join(a for a in (" --quick" if args.quick else "", f" --embedder {args.embedder}")),
    }
    report = render_report(results, samples, meta, args.k, args.candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(results_table(results, args.k))
    print(f"\nFull report: {args.output}")


if __name__ == "__main__":
    main()
