# Chunking experiment

- **Date:** 2026-11-07 04:56 UTC
- **Grid:** full (18 configurations)
- **Corpus:** 6 true documents (0.10M chars), 57 noisy documents (4.82M chars)
  > **The distractor corpus used for this run is no longer in the repository.** Those 57 documents were
  > third-party material that cannot be redistributed, so they were removed (see
  > [DATA/README.md](../../DATA/README.md)). This run cannot be reproduced from a fresh clone: re-running
  > the experiment with a different distractor corpus will give different absolute numbers, and with no
  > distractors at all the noise rate column is 0 by construction. The comparison between chunking
  > configurations below is unaffected, because every configuration saw the same corpus.
- **Questions:** 15 golden questions (`evals/golden_dataset.json`); one question = 6.7 points of hit rate
- **Dense embedder:** `sentence-transformers/all-MiniLM-L6-v2 (local, max 256 tokens)` (384-dim), cached per unique text
- **Sparse:** fastembed `Qdrant/bm25`, Qdrant IDF modifier
- **Retrieval:** Qdrant in-memory (`QdrantClient(":memory:")`), dense + BM25 prefetch fused with RRF,
  top 15 candidates → FlashRank (`ms-marco-TinyBERT-L-2-v2`) → top 5: the retriever node's pipeline
- **Semantic chunker:** breakpoint at the 10th percentile of neighbour-sentence
  cosine similarity (per document), minimum chunk 50% of the size; sentences
  embedded with the same model
- **Hit rule:** `chunk_covers()` in `evals/retrieval_metrics.py`: containment, or ≥ 8 shared
  word bigrams that are ≥ 50% of the smaller text's bigrams (corpus check in its
  docstring). "Strict hit" uses the production benchmark rule `chunk_matches()`.

> **Embedder caveat.** These numbers were produced with `sentence-transformers/all-MiniLM-L6-v2 (local, max 256 tokens)`, not the production Gemini
> model, so absolute values will differ with `--embedder gemini`. What we compare is the relative ranking of
> chunking configurations under one fixed retrieval stack.

## Results (sorted by MRR@5)

| # | Strategy | Size | Overlap | Chunks | Avg len | Hit@5 | MRR@5 | ΔMRR vs best (95% CI) | Strict hit@5 | Coverage@5 | Noise@5 ↓ | Ctx chars@5 | Embedded chars | Index s |
|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | fixed | 1500 | 225 (15%) | 3,872 | 1487 | 1.00 | 0.967 | best | 1.00 | 0.96 | 0.01 | 7,142 | 5.8M | 42 |
| 2 | paragraph (current default) | 1500 | 0 | 3,340 | 1471 | 1.00 | 0.900 | -0.067 [-0.17, +0.00] within noise | 1.00 | 0.94 | 0.00 | 7,249 | 4.9M | 35 |
| 3 | fixed | 1500 | 0 | 3,320 | 1480 | 1.00 | 0.900 | -0.067 [-0.17, +0.00] within noise | 1.00 | 0.95 | 0.00 | 7,444 | 4.9M | 25 |
| 4 | recursive | 1000 | 150 (15%) | 6,172 | 936 | 0.93 | 0.900 | -0.067 [-0.23, +0.07] within noise | 0.87 | 0.89 | 0.00 | 4,555 | 5.8M | 88 |
| 5 | paragraph | 1000 | 0 | 5,001 | 982 | 0.93 | 0.900 | -0.067 [-0.23, +0.07] within noise | 0.87 | 0.87 | 0.00 | 4,863 | 4.9M | 48 |
| 6 | fixed | 1000 | 0 | 4,963 | 990 | 0.93 | 0.867 | -0.100 [-0.27, +0.07] within noise | 0.87 | 0.85 | 0.01 | 4,971 | 4.9M | 44 |
| 7 | semantic | 1500 | 0 | 4,486 | 1095 | 0.93 | 0.867 | -0.100 [-0.27, +0.07] within noise | 0.93 | 0.89 | 0.01 | 5,501 | 9.7M | 39 |
| 8 | recursive | 1500 | 225 (15%) | 4,066 | 1423 | 1.00 | 0.856 | -0.111 [-0.27, +0.03] within noise | 1.00 | 0.95 | 0.00 | 6,991 | 5.8M | 40 |
| 9 | paragraph | 500 | 0 | 9,993 | 491 | 0.93 | 0.847 | -0.120 [-0.27, +0.00] within noise | 0.80 | 0.80 | 0.00 | 2,426 | 4.9M | 70 |
| 10 | recursive | 1500 | 0 | 3,470 | 1416 | 1.00 | 0.822 | -0.144 [-0.28, -0.03] worse | 1.00 | 0.95 | 0.00 | 6,804 | 4.9M | 35 |
| 11 | recursive | 1000 | 0 | 5,306 | 925 | 1.00 | 0.811 | -0.156 [-0.31, +0.00] within noise | 0.93 | 0.90 | 0.00 | 4,589 | 4.9M | 61 |
| 12 | fixed | 500 | 0 | 9,916 | 495 | 0.87 | 0.800 | -0.167 [-0.33, -0.03] worse | 0.67 | 0.76 | 0.00 | 2,467 | 4.9M | 57 |
| 13 | fixed | 500 | 75 (15%) | 11,563 | 495 | 0.87 | 0.778 | -0.189 [-0.39, -0.02] worse | 0.87 | 0.84 | 0.00 | 2,469 | 5.7M | 105 |
| 14 | fixed | 1000 | 150 (15%) | 5,798 | 992 | 0.93 | 0.750 | -0.217 [-0.40, -0.03] worse | 0.87 | 0.88 | 0.00 | 4,898 | 5.8M | 73 |
| 15 | semantic | 500 | 0 | 11,830 | 414 | 0.80 | 0.672 | -0.294 [-0.51, -0.10] worse | 0.80 | 0.77 | 0.00 | 2,101 | 9.7M | 279 |
| 16 | semantic | 1000 | 0 | 6,324 | 776 | 0.93 | 0.661 | -0.306 [-0.48, -0.14] worse | 0.93 | 0.88 | 0.00 | 3,956 | 9.7M | 71 |
| 17 | recursive | 500 | 0 | 10,968 | 447 | 0.73 | 0.578 | -0.389 [-0.59, -0.20] worse | 0.73 | 0.75 | 0.00 | 2,217 | 4.9M | 99 |
| 18 | recursive | 500 | 75 (15%) | 12,755 | 452 | 0.87 | 0.552 | -0.414 [-0.59, -0.23] worse | 0.80 | 0.78 | 0.00 | 2,153 | 5.8M | 137 |

Column notes: **ΔMRR vs best**: paired bootstrap over the 15 questions; an interval containing 0 means the
configuration can't be told apart from the best on this dataset. **Coverage@5**: share of the golden
contexts' word bigrams found in the top 5 (how much of the evidence the LLM receives). **Ctx chars@5**:
characters handed to the LLM per question (cost proxy). **Embedded chars**: characters sent to the dense
embedder at ingestion with a cold cache (semantic includes the sentence embeddings). **Index s**: chunk +
embed + upsert wall time in this run; lower when an earlier configuration already embedded the same texts
(notably semantic at 1000 and 1500, whose sentence vectors were cached at 500).

## By strategy

| Strategy | Configs | Mean hit@5 | Mean MRR@5 | Mean coverage@5 | Mean ctx chars@5 |
|---|---:|---:|---:|---:|---:|
| paragraph | 3 | 0.96 | 0.882 | 0.87 | 4,846 |
| fixed | 6 | 0.93 | 0.844 | 0.87 | 4,898 |
| recursive | 6 | 0.92 | 0.753 | 0.87 | 4,551 |
| semantic | 3 | 0.89 | 0.733 | 0.85 | 3,853 |

## By chunk size

| Chunk size | Configs | Mean hit@5 | Mean MRR@5 | Mean coverage@5 | Mean ctx chars@5 |
|---|---:|---:|---:|---:|---:|
| 500 | 6 | 0.84 | 0.704 | 0.78 | 2,306 |
| 1000 | 6 | 0.94 | 0.815 | 0.88 | 4,639 |
| 1500 | 6 | 0.99 | 0.885 | 0.94 | 6,855 |

## Effect of overlap (same strategy and size)

| Strategy | Size | MRR@5 0 → 15% | Hit@5 0 → 15% | Coverage@5 0 → 15% | Chunks 0 → 15% |
|---|---:|---|---|---|---|
| fixed | 500 | 0.800 → 0.778 | 0.87 → 0.87 | 0.76 → 0.84 | 9,916 → 11,563 (+17%) |
| recursive | 500 | 0.578 → 0.552 | 0.73 → 0.87 | 0.75 → 0.78 | 10,968 → 12,755 (+16%) |
| fixed | 1000 | 0.867 → 0.750 | 0.93 → 0.93 | 0.85 → 0.88 | 4,963 → 5,798 (+17%) |
| recursive | 1000 | 0.811 → 0.900 | 1.00 → 0.93 | 0.90 → 0.89 | 5,306 → 6,172 (+16%) |
| fixed | 1500 | 0.900 → 0.967 | 1.00 → 1.00 | 0.95 → 0.96 | 3,320 → 3,872 (+17%) |
| recursive | 1500 | 0.822 → 0.856 | 1.00 → 1.00 | 0.95 → 0.95 | 3,470 → 4,066 (+17%) |

## Rank of the first relevant chunk per question

Top configurations and the current default (`–` = no relevant chunk in the top 5).

| # | Question | fixed/1500/225 | paragraph/1500/0 | fixed/1500/0 | recursive/1000/150 | paragraph/1000/0 |
|---|---|---:|---:|---:|---:|---:|
| 1 | How do you start Redis for a Kubernetes work queue? | 1 | 1 | 1 | 1 | 1 |
| 2 | What does the parallelism field do in the Kubernetes job-... | 1 | 1 | 1 | 1 | 2 |
| 3 | How do you fill the Redis work queue with tasks using the... | 1 | 1 | 1 | 1 | 1 |
| 4 | What is the difference between HPA and VPA in Kubernetes? | 1 | 1 | 1 | 1 | 1 |
| 5 | How do you install the Metrics Server for Kubernetes pod ... | 2 | 2 | 2 | 1 | 1 |
| 6 | Which kubectl commands confirm that the Metrics Server is... | 1 | 1 | 1 | 1 | 1 |
| 7 | What Databricks CLI command retrieves the details of a sp... | 1 | 1 | 1 | 1 | 1 |
| 8 | When should you use the Databricks REST API instead of th... | 1 | 1 | 1 | 1 | 1 |
| 9 | What are the three main developer tools for managing Data... | 1 | 1 | 1 | 1 | 1 |
| 10 | What are the valid restart policy values for a Kubernetes... | 1 | 1 | 1 | 1 | 1 |
| 11 | What are the completions and parallelism fields in a Kube... | 1 | 2 | 2 | – | 1 |
| 12 | Which Kubernetes API group does a Job resource belong to? | 1 | 1 | 1 | 1 | – |
| 13 | How do you start a local Kubernetes cluster using Minikube? | 1 | 1 | 1 | 1 | 1 |
| 14 | What kubectl command checks the status of a running Kuber... | 1 | 2 | 2 | 2 | 1 |
| 15 | How do you verify that the core Kubernetes components are... | 1 | 1 | 1 | 1 | 1 |

## Findings

_Written by hand from the 2026-11-07 run above. Re-running the script regenerates the tables and resets
this section._

**Read every difference against the noise floor.** 15 questions: one question moves hit rate by 6.7 points,
and moving one question from rank 2 to rank 1 moves MRR by 0.033. 9 of the 18 configurations cannot be told
apart from the best (their 95% bootstrap interval for ΔMRR contains 0).

1. **Best configuration: `fixed / 1500 / 225`** (MRR 0.967, hit 1.00). It is **not distinguishably better than
   the current default `paragraph / 1500 / 0`** (MRR 0.900, hit 1.00): ΔMRR −0.067, 95% CI [−0.17, +0.00]. The
   whole difference is two questions (11 and 14) whose first relevant chunk moves from rank 2 to rank 1.

2. **Chunk size is the clearest effect.** Mean over strategies at 500 / 1000 / 1500: MRR 0.704 / 0.815 / 0.885,
   hit rate 0.84 / 0.94 / 0.99, coverage 0.78 / 0.88 / 0.94. For every strategy and overlap, 1500 beats 500 on MRR.
   The price is context: about 2,300 / 4,600 / 6,900 characters handed to the LLM per question (3× from 500 to
   1500). Caveats: (a) 1500 is the edge of the grid, so larger sizes are untested; (b) the golden contexts are
   short (162–528 chars, median 269) single-passage lookups, which a large chunk contains whole, so this dataset
   favours large chunks and says nothing about multi-passage questions; (c) MiniLM embeds only the first 256 tokens
   (about 1,000 characters of prose, less for YAML), which works *against* large chunks in the dense leg, and large
   chunks won anyway.

3. **Small chunks lose context, and the hit rule shows it.** At 500 the strict production rule misses hits that
   the partial rule counts (fixed/500/0: strict hit 0.67 vs partial 0.87; paragraph/500: 0.80 vs 0.93). The chunk
   is the right passage but holds only part of the evidence, so coverage@5 drops to 0.75–0.84.

4. **Overlap: no consistent benefit at this sample size, at a fixed cost.** MRR changes 0 → 15%: −0.02, −0.03,
   −0.12, +0.09, +0.07, +0.03. The signs disagree, and each change is at most 0.12 MRR, under two questions' worth of reciprocal rank. Where
   overlap is expected to help, small chunks cut at arbitrary points, coverage does rise (fixed/500: 0.76 → 0.84),
   and recursive/500 gains two hits (0.73 → 0.87). The cost is certain: +16–17% chunks, embeddings and vector
   storage. Context per query doesn't change (always the top 5).

5. **Semantic chunking costs the most and wins nothing here.** It has the lowest mean MRR (0.733). At 500 and 1000
   it is worse than the best beyond noise (CIs exclude 0). At 1500 (MRR 0.867) it is within noise. It embeds about
   twice as many characters at ingestion (9.7M vs 4.9M, every sentence plus every chunk), and its cold-cache
   indexing took 279 s vs 57–137 s for the others at the same size.

6. **Recursive is worst at small sizes** (500: MRR 0.578 / 0.552, the bottom two rows). Not diagnosed. Note that
   4 of the 6 golden documents (DOCX, HTML) contain no blank lines, so recursive falls through to line breaks
   there. Recursive/1000/150 (MRR 0.900) is within noise of the best.

7. **On this corpus, `paragraph` behaves like `fixed`.** The parsers emit no blank lines for DOCX and HTML, so the
   paragraph chunker sees each of those documents as one paragraph and cuts it at spaces. paragraph/1500/0 and
   fixed/1500/0 have identical per-question ranks.

8. **Noise rate doesn't discriminate.** It is 0.00–0.01 everywhere: hybrid retrieval plus FlashRank keeps the
   distractor corpus out of the top 5 whatever the chunking.

### Recommendation

**Keep the current default, `paragraph / 1500 / 0`.** It ties for the best hit rate (1.00), sits within noise
of the top MRR, embeds the fewest characters and needs no overlap. The top-ranked `fixed / 1500 / 225` beats it
by two rank-2 → rank-1 moves, which is inside the noise, and costs 17% more chunks. That isn't enough evidence to
re-ingest. Before changing the default:
- rerun the top three configurations with `--embedder gemini` (the production embedder, no 256-token truncation);
- extend the size grid beyond 1500;
- add questions whose evidence spans more than one passage, so the comparison isn't limited to short
  single-passage lookups.

Generated by `python -m evals.chunking_experiment --embedder local`.
