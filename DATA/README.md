# Sample corpus

This folder holds the documents the service indexes in development and in the evaluations.

Sub-folder names become the `source_type` stored on every chunk:

| Folder | `source_type` | Role | In this repository? |
|---|---|---|---|
| `true_data/` | `true` | Curated documents that the golden questions are answered from | Yes, 6 files |
| `noisy_data/` | `noisy` | Distractor documents: plausible but unrelated technical material that retrieval must rank below the curated documents | **No, see below** |

Index everything with:

```bash
python -m app.ingestion.processor DATA --wipe
```

A folder that is missing is skipped with a message, so ingestion, the chunking experiment and the evaluation parser all run with `true_data/` alone.

## The distractor corpus is not distributed

`noisy_data/` is empty in this repository. It previously held 58 third-party files (97 MB): conference papers, vendor white papers, slide decks and complete books. They are not ours to redistribute — no licence was granted for them — so they have been removed from the tracked files and `DATA/noisy_data/` is in `.gitignore`.

### Supplying your own distractors

Put any documents you have the right to use into `DATA/noisy_data/` and re-ingest. They stay local; the folder is ignored by git, Docker and gcloud.

- Supported formats: PDF, HTML, TXT, DOCX, PPTX.
- Sub-folders are not scanned; files go directly in the folder.
- They should be plausible neighbours of the curated documents (general infrastructure, systems or computer-science material) but must not answer the golden questions, or they stop being distractors.
- Openly licensed sources that work well: [arXiv](https://arxiv.org/) papers whose licence permits redistribution, [Wikipedia](https://en.wikipedia.org/) articles (CC BY-SA 4.0), and documentation published under Creative Commons such as the [Kubernetes documentation](https://kubernetes.io/docs/) (CC BY 4.0).
- Around 50 documents of a few hundred thousand characters in total is enough to make noise rate@5 meaningful.

### What this changes in the evaluations

- **Noise rate@5** (`evals/retrieval_metrics.py`, reported by the retrieval benchmark and the chunking experiment) is the share of top-5 chunks that come from the distractor corpus. With no distractors indexed it is `0.00` by construction, whatever the retrieval quality. It is not a result.
- **Hit rate, MRR and coverage** are unaffected in definition, but they are easier to score without distractors competing for the top 5, so they are not comparable with the published numbers.
- The published [chunking experiment](../evals/results/chunking_experiment.md) (run on 2026-11-07) used the removed corpus: "6 true documents (0.10M chars), 57 noisy documents (4.82M chars)". **Those numbers cannot be reproduced from this repository.** Re-running the experiment with a different distractor corpus will produce different absolute values; the report states this.

## Where `true_data/` came from

These six files are third-party material too, kept for now because the 15 golden questions in [`evals/golden_dataset.json`](../evals/golden_dataset.json) are built from them. Only the first is openly licensed.

| File | Source | Licence |
|---|---|---|
| `parallel_work_queue.txt` | Kubernetes documentation, "Fine Parallel Processing Using a Work Queue" | CC BY 4.0 |
| `pods_autoscale.html` | Third-party blog article, "A Hands-On Guide to Kubernetes Horizontal & Vertical Pod Autoscalers" (2024) | Not open |
| `cronjobs.docx` | Third-party article, "Mastering Kubernetes Jobs and CronJobs: A Platform Engineer's Complete Guide" | Not open |
| `monitor_job.docx` | Third-party article, "How to monitor Kubernetes job status and logs" | Not open |
| `job_management.html` | Databricks documentation, "Automate job creation and management" | Not open |
| `architecture.pptx` | Third-party slide deck, "Kubernetes: A Comprehensive Overview" (Kubernetes v1.8) | Unknown |

Every file except `parallel_work_queue.txt` will be replaced with openly licensed material — the Kubernetes documentation covers the same topics and is CC BY 4.0 — when the golden dataset is rebuilt in [issue #71](https://github.com/anilgehlotn/enterprise-agentic-rag/issues/71). Replacing them means regenerating the golden questions and re-running the chunking experiment and the retrieval benchmark, which is why it is one piece of work rather than a file swap.

`processed_data/` holds the parsed and chunked output of whatever is ingested, written by `app/ingestion/processor.py` and read by the `list_indexed_documents` MCP tool. `processed_data/true/` is tracked; `processed_data/noisy/` is ignored, like its source folder.

## Licensing

No licence has been applied to this repository yet (see the README), and the documents in this folder are in any case not covered by one: each belongs to its own author or publisher and is kept here for development and evaluation only.
