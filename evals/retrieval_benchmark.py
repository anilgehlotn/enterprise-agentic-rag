"""Retrieval benchmark: dense vs hybrid (BM25 + RRF), with and without FlashRank.

Runs the 15 golden questions (evals/golden_dataset.json → rag_samples) against the
LIVE Qdrant collection and compares four configurations:

    a) Dense only            dense top-k
    b) Dense + FlashRank     dense top-N candidates → FlashRank → top-k   (agent with RETRIEVAL_MODE=dense)
    c) Hybrid (RRF)          dense + BM25 prefetch, Qdrant RRF fusion → top-k
    d) Hybrid + FlashRank    hybrid top-N candidates → FlashRank → top-k  (the agent's default pipeline)

N = --candidates (default 15, the retriever node's value), k = --k (default 5).
For each question one dense and one hybrid search are run; (a)/(c) take their first
k results and (b)/(d) rerank the same candidate list, so the reranked and
non-reranked rows see identical candidates.

Metrics (definitions and the chunk/golden-context match rule: evals/retrieval_metrics.py):
    Hit rate@k, MRR@k, Noise rate@k (share of top-k chunks from the "noisy" corpus),
    and average latency per query in ms. Latency = the Qdrant search, including
    the query embedding (Gemini; plus BM25 for hybrid), and for (b)/(d) the FlashRank
    rerank on top. Model loading is done in an untimed warm-up first.

Rate limits: every search makes one Gemini embedding call. The benchmark sleeps
--delay seconds (default 1.5) after each search and retries a failed search with
exponential backoff, so 30 searches stay well under Gemini's per-minute quota.

Run (requires .env with GEMINI_API_KEY, QDRANT_CLUSTER_ENDPOINT, QDRANT_API_KEY and a
collection ingested with the hybrid schema):

    python -m evals.retrieval_benchmark
    python -m evals.retrieval_benchmark --k 5 --candidates 15 --delay 2

Prints the comparison table and writes it to evals/results/retrieval_benchmark.md.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from evals.retrieval_metrics import (
    first_relevant_rank,
    hit_rate,
    mean_reciprocal_rank,
    noise_rate,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals" / "golden_dataset.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "retrieval_benchmark.md"

MAX_SEARCH_ATTEMPTS = 4
BACKOFF_SECONDS = 5


@dataclass(frozen=True)
class Config:
    key: str
    label: str
    mode: str
    rerank: bool


CONFIGS = (
    Config("dense", "Dense only", "dense", False),
    Config("dense_rerank", "Dense + FlashRank", "dense", True),
    Config("hybrid", "Hybrid (RRF)", "hybrid", False),
    Config("hybrid_rerank", "Hybrid + FlashRank", "hybrid", True),
)


@dataclass
class ConfigResult:
    ranks: list[int | None] = field(default_factory=list)
    source_types: list[list[str]] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    def add(self, records: list[dict], golden: list[str], latency_ms: float, k: int) -> None:
        top = records[:k]
        self.ranks.append(first_relevant_rank([r.get("content", "") for r in top], golden, k))
        self.source_types.append([r.get("source_type", "unknown") for r in top])
        self.latencies_ms.append(latency_ms)

    def summary(self, k: int) -> dict:
        n = len(self.latencies_ms)
        return {
            "hit_rate": hit_rate(self.ranks),
            "mrr": mean_reciprocal_rank(self.ranks),
            "noise_rate": noise_rate(self.source_types, k),
            "latency_ms": sum(self.latencies_ms) / n if n else 0.0,
        }


def load_samples(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [s for s in data["rag_samples"] if s.get("question") and s.get("relevant_contexts")]


def _search_with_retry(search, query: str, mode: str, limit: int):
    """Timed search; retries RetrievalError (e.g. a Gemini 429) with exponential backoff."""
    from app.services.retrieval.schema import CollectionSchemaError, RetrievalError

    for attempt in range(MAX_SEARCH_ATTEMPTS):
        start = time.perf_counter()
        try:
            records = search(query, limit=limit, mode=mode)
            return records, (time.perf_counter() - start) * 1000
        except CollectionSchemaError:
            raise
        except RetrievalError as exc:
            if attempt == MAX_SEARCH_ATTEMPTS - 1:
                raise
            wait = BACKOFF_SECONDS * 2**attempt
            print(f"  search failed ({exc.__cause__ or exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def run_benchmark(
    samples: list[dict], k: int = 5, candidates: int = 15, delay: float = 1.5, search=None, rerank=None
) -> tuple[dict[str, ConfigResult], list[dict]]:
    """Runs every sample through the four configurations. search/rerank are injectable for tests."""
    if search is None:
        from app.services.retrieval.qdrant_service import search_enterprise_knowledge as search
    if rerank is None:
        from app.services.retrieval.ranking_service import rerank_records as rerank

    # Untimed warm-up: Gemini probe, BM25 model and FlashRank model load once here.
    for mode in ("dense", "hybrid"):
        warm, _ = _search_with_retry(search, "kubernetes job", mode, candidates)
        rerank("kubernetes job", warm, top_n=k)
        time.sleep(delay)

    results = {config.key: ConfigResult() for config in CONFIGS}
    per_question = []
    for index, sample in enumerate(samples, start=1):
        question, golden = sample["question"], sample["relevant_contexts"]
        print(f"[{index}/{len(samples)}] {question}", file=sys.stderr)
        row = {"id": sample.get("id", index), "question": question}

        for mode in ("dense", "hybrid"):
            records, search_ms = _search_with_retry(search, question, mode, candidates)
            time.sleep(delay)

            start = time.perf_counter()
            reranked = rerank(question, records, top_n=k)
            rerank_ms = (time.perf_counter() - start) * 1000

            for config in CONFIGS:
                if config.mode != mode:
                    continue
                result = results[config.key]
                if config.rerank:
                    result.add(reranked, golden, search_ms + rerank_ms, k)
                else:
                    result.add(records, golden, search_ms, k)
                row[config.key] = result.ranks[-1]
        per_question.append(row)

    return results, per_question


# ── Reporting ─────────────────────────────────────────────────────────────────


def comparison_table(results: dict[str, ConfigResult], k: int) -> str:
    lines = [
        f"| Configuration | Hit rate@{k} | MRR@{k} | Noise rate@{k} ↓ | Avg latency (ms) |",
        "|---|---:|---:|---:|---:|",
    ]
    for config in CONFIGS:
        s = results[config.key].summary(k)
        lines.append(
            f"| {config.label} | {s['hit_rate']:.2f} | {s['mrr']:.3f} | {s['noise_rate']:.2f} | {s['latency_ms']:.0f} |"
        )
    return "\n".join(lines)


def per_question_table(per_question: list[dict]) -> str:
    header = "| # | Question | " + " | ".join(c.label for c in CONFIGS) + " |"
    lines = [header, "|---|---|" + "---:|" * len(CONFIGS)]
    for row in per_question:
        question = row["question"] if len(row["question"]) <= 70 else row["question"][:67] + "..."
        ranks = " | ".join("–" if row[c.key] is None else str(row[c.key]) for c in CONFIGS)
        lines.append(f"| {row['id']} | {question} | {ranks} |")
    return "\n".join(lines)


def collection_metadata() -> dict:
    from app.config import settings
    from app.services.retrieval.embedding import get_embedding_model_name
    from app.services.retrieval.qdrant_service import get_qdrant_client
    from app.services.retrieval.schema import BM25_MODEL

    client = get_qdrant_client()
    return {
        "collection": settings.QDRANT_COLLECTION,
        "points": client.count(settings.QDRANT_COLLECTION, exact=True).count,
        "dense_model": get_embedding_model_name(),
        "sparse_model": BM25_MODEL,
    }


def render_report(results, per_question, meta: dict, k: int, candidates: int, n_questions: int) -> str:
    return f"""# Retrieval benchmark

- **Date:** {meta["date"]}
- **Collection:** `{meta["collection"]}` ({meta["points"]} chunks)
- **Dense embedding model:** `{meta["dense_model"]}`
- **Sparse model:** `{meta["sparse_model"]}` (Qdrant IDF modifier)
- **Questions:** {n_questions} golden questions (`evals/golden_dataset.json`)
- **k:** {k} &nbsp;·&nbsp; **FlashRank candidate pool:** {candidates}

{comparison_table(results, k)}

Hit rate and MRR: higher is better. Noise rate = share of top-{k} chunks from the distractor
(`noisy`) corpus: lower is better. Latency includes query embedding and, for FlashRank rows,
the rerank. Match rule: `evals/retrieval_metrics.py`.

## Rank of the first relevant chunk per question

(`–` = no relevant chunk in the top {k})

{per_question_table(per_question)}

Generated by `python -m evals.retrieval_benchmark`.
"""


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.retrieval_benchmark", description=__doc__.split("\n")[0])
    parser.add_argument("--k", type=int, default=5, help="cut-off for hit rate / MRR / noise rate (default 5)")
    parser.add_argument("--candidates", type=int, default=15, help="candidates fetched before FlashRank (default 15)")
    parser.add_argument(
        "--delay", type=float, default=1.5, help="seconds to sleep after each search (Gemini rate limit)"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    import logfire

    logfire.configure(send_to_logfire="if-token-present", service_name="retrieval-benchmark", console=False)

    samples = load_samples(args.dataset)
    results, per_question = run_benchmark(samples, k=args.k, candidates=args.candidates, delay=args.delay)

    meta = collection_metadata()
    meta["date"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    report = render_report(results, per_question, meta, args.k, args.candidates, len(samples))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print()
    print(comparison_table(results, args.k))
    print(f"\nFull report: {args.output}")


if __name__ == "__main__":
    main()
