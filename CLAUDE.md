# Vidhyasathi — Project Context & Working Brief

> This file is the single source of truth for the project's *intent, decisions, and
> state*. It exists so any Claude assistant (in Claude Code / VS Code) can continue
> the work as a seamless continuation of the conversation that built it. Read it
> fully before acting. The code is the ground truth for *how*; this file is the
> ground truth for *why* and *what next*.

---

## 0. TL;DR — where we are right now

**Vidhyasathi** is an intelligent, voice-or-text study assistant that answers *grounded
in the student's own material*, teaches, examines, and — crucially — **knows when to
say "I don't know" and hand off to a human.** Core thesis: **answer when grounded, be
honest when unsure, escalate to a human when it matters.**

**Built so far (the "Foundation" spine, working end-to-end):**
a size-aware Retrieval-Augmented Generation (RAG) pipeline over a multi-format document
set, producing grounded, cited answers and honest refusals.

`ingest → chunk → embed → store(Qdrant) → retrieve → re-rank → answer(LLM)`

**Immediate next step:** verify the just-finished multi-format ingest on a real mixed
document set (build the KB, confirm citations name the right source + locator, confirm
the large-KB branch triggers). **Then** make the confidence gate *drive behaviour*
(answer / refuse / offer "ask a human") — that gate is the branch point for the modes
and escalation. See §11 Roadmap.

**Working style (important — honour this):** step-by-step, **one command at a time**,
confirm before proceeding on decisions. When code changes, **hand over the FULL file,
not a diff.** Be concise and direct. Be an honest engineer: call out regressions and
trade-offs, never oversell a result. Any human-facing text should read human-written.

---

## 1. The product vision (the full thing we're building toward)

A student uses Vidhyasathi by **voice or text**. It behaves less like a search box and
more like a good teacher. Everything is **grounded in the student's own institution**
(their notes, their syllabus, their past papers, their teachers).

### 1.1 Three modes (chosen from a dropdown — each reconfigures behaviour, like picking a model)
- **Quick Answer** — fast, grounded reply with a self-check. *(the spine we've built)*
- **Teach Me** — step-by-step, adaptive, prerequisite-aware guided learning. Mostly a
  different system prompt + conversation memory over the same engine.
- **Exam & Practice** — generate exams from the material + past papers; student answers
  online *or on paper* (written answers + diagrams photographed/uploaded); rubric-based
  **explained feedback (NOT final marks — positioned honestly).** The written/diagram
  grading needs OCR + a vision model — its own sub-project.

### 1.2 Layered grounding (always show which layer answered)
1. Student's own notes (highest trust; cite to exact locator)
2. Curated syllabus knowledge base (vetted textbooks/course material/past papers)
3. Open-source / web retrieval (OpenStax, NPTEL, Wikipedia, search API) — *still grounded*
4. The model's own parametric knowledge — **the true last resort, labelled least-certain**

### 1.3 Confidence gate → answer or escalate
Decide answer-directly vs. offer an **"Ask for Help"** escalation. Available in all modes.

### 1.4 Ask for Help / human routing (the other half of the gate)
When confidence is low (or the student asks): create a doubt ticket, **semantically match
it to the right helper** (teacher / TA / senior), the helper sees the AI's draft + sources,
and **every human answer feeds back into the KB** — so escalations fall over time.

### 1.5 Honest framing (targets, not guarantees)
≥90% answer accuracy on grounded questions; ≥0.80 retrieval MRR vs a keyword baseline;
a measurable **decline in escalations** over a term. These are goals to measure, not promises.

---

## 2. Tech stack & hardware

- **Language:** Python **3.11.9** (note: 3.11 f-string rule in §9).
- **Embeddings:** `sentence-transformers` — `all-MiniLM-L6-v2` (384-dim, normalised).
- **Re-ranker:** `cross-encoder/ms-marco-MiniLM-L-6-v2`.
- **Vector store:** **Qdrant**, local **on-disk** mode (no server, no Docker) at `qdrant_storage/`.
- **LLM:** **Ollama**, `llama3.1:8b`, local. OpenAI-style API, so cloud swap is one line later.
- **Document loaders:** `pypdf`, `python-docx`, `python-pptx`, `openpyxl` (+ stdlib `csv`).
- **UI (later):** Streamlit or React chat app.
- **Later sub-systems:** `faster-whisper` (voice STT), OCR + vision model (exam grading),
  MySQL (profiles / tickets / logs).
- **Hardware:** dev on **Mac M4 Pro** (runs 7–8B models comfortably); a **Windows RTX 4060**
  box is the GPU server for bigger models later; Google Colab as an option for heavy jobs.

---

## 3. Architecture / pipeline (each stage = one module, composable)

Records flow through stages, each taking the previous stage's output. The common record
shapes are the contract that keeps stages decoupled:

- ingest emits: `{loc, order, text}`
- chunk emits:  `{id, source, loc, order, text}`
- retrieve/rerank emit: `{score, source, loc, order, chunk_id, text}` (+ `rerank_score`, `blend_score`)

`loc` is a **generalised locator** for citations — `p3` (PDF page), `slide 4` (PPTX), a
heading (DOCX), `Marks row 2` (XLSX). It replaced the old bare integer "page" because a
page number is meaningless for a slide deck or a spreadsheet. `order` is an int for stable
in-document sorting.

---

## 4. File-by-file map (`src/`)

- **`config.py`** — every tunable path, model name, threshold. Nothing is hard-coded
  elsewhere. Change models/thresholds here only. (Full reference in §7.)
- **`ingest.py`** — a **loader registry** (`LOADERS` dict: extension → loader fn). Handles
  `.pdf .docx .pptx .txt .md .csv .xlsx`. `list_documents()`, `load_document(path)`,
  `unsupported_files()`, `source_label(path)`, `clean(text)`. DOCX is segmented by heading
  (loc = heading); PPTX per slide; XLSX/CSV serialise each row as `"Header: value; ..."`
  so tables embed and retrieve well. **Not handled yet:** `.doc/.xls`, scanned PDFs/images
  (need OCR — the vision sub-project). `build_index` reports these as "skipped".
- **`chunk.py`** — `chunk_records(records, source)` → overlapping windows. `chunk_text`
  backs up to the last space so words aren't cut. IDs are `"{slug}_{order}_c{j}"`, unique
  across documents so chunks never collide.
- **`embed.py`** — `get_model()` (cached), `embed_texts()`, `embed_query()`. Normalised
  embeddings so dot-product == cosine.
- **`store.py`** — Qdrant local on-disk. `get_client()`, `close()` (releases the on-disk
  lock — see §9), `reset_collection()`, `upsert_chunks()`, `count()`. Payload per point:
  `{chunk_id, source, loc, order, text}`.
- **`retrieve.py`** — embeds the query, `query_points`, **caps limit at KB size**
  (adaptive to document size). Returns scored hits with source/loc.
- **`rerank.py`** — cross-encoder scores `(question, chunk)` pairs, then **blends**:
  `0.7*minmax(rerank) + 0.3*minmax(cosine)` (see §6). Keeps best `TOP_K`.
- **`answer.py`** — **size-aware** (see §6): small KB → feed the whole document in
  `(source, order)` order (no pruning); large KB → retrieve wide → blended re-rank → top_k.
  `ask_llm()` is the **only** seam that talks to the model. `SYSTEM_PROMPT` forces
  grounding, citation of the `[source · loc]` tag, and the exact refusal
  `"I don't have that in the material."` Confidence gate flags `weak` when top cosine
  `< MIN_RETRIEVAL_SCORE`.
- **`build_index.py`** — **THE single entry point to (re)build the KB** from `data/docs/`:
  folder → chunks (source-tagged) → embed all → store. Run after adding/changing docs.

Layout:
```
vidhyasathi/
├── src/  (config, ingest, chunk, embed, store, retrieve, rerank, answer, build_index, __init__)
├── data/docs/        # source documents (git-ignored; copyrighted texts stay local)
├── qdrant_storage/   # local vector DB (git-ignored)
├── requirements.txt
├── .gitignore
└── CLAUDE.md         # this file
```

---

## 5. How to run

```bash
source .venv/bin/activate          # venv is at .venv, VS Code interpreter points to it
python -m src.build_index          # (re)build KB from everything in data/docs/
python -m src.answer <question>    # ask a grounded question (no quotes needed)
```
Always run modules as `python -m src.X` (not `python src/X.py`) so `from src....` imports
resolve. Ollama must be running (the app; service on `localhost:11434`).

---

## 6. Key design decisions & the reasoning (the "back-and-forth")

**Small/fast models first, on purpose (not timidity).**
- Iterate fast while the pipeline is unproven; a 70B model would crawl on the Mac *and*
  hide which stage is actually failing.
- **RAG lowers the bar on the LLM** — it rephrases retrieved text, not recalls facts, so
  7–8B models do it well. Much "we need a bigger model" is really "our retrieval was bad."
- The **embedder sets the vector dimension** (384) — an ongoing storage/speed cost while
  re-ingesting repeatedly. Quality comes more from the **re-ranker** than from a huge embedder.
- Everything is swappable in **one config line** (`EMBED_MODEL`/`LLM_MODEL`/`RERANK_MODEL`),
  and model calls sit behind thin wrappers — so there's no lock-in to starting small.

**When do we swap to bigger models? Only when a measurement points at a stage.**
- Build a 30–50 question eval set (known answers + known locators) and use **RAGChecker**
  (source #5) to score retrieval vs. generation separately.
- Diagnose before swapping: **low retrieval recall → upgrade the embedder**; **right passage
  retrieved but ranked low → strengthen the re-ranker**; **correct context but bad answer →
  upgrade the LLM**. Most "need a bigger model" is actually a retrieval problem.
- Also legitimate: swap the LLM up *just for a demo/review*, or when a measured hard limit is hit.

**Size-aware context (retrieval is a workaround for docs too big to read whole).**
- On a **small KB** (≤ `SMALL_KB_MAX` chunks) we feed the model the **entire document in
  page/order sequence** — no pruning. On a **large KB** we use retrieve → re-rank → top_k.
- *Why:* we observed a real regression — cutting a 16-chunk syllabus to top-5 dropped the
  page that introduced Power BI/Tableau, and the model then **mis-cited** them. Feeding the
  whole small doc fixed the citations *and* the honesty guard still held (it refused
  out-of-material questions even with the whole doc in context).
- This is **invisible to the user**: they just import documents; the pipeline picks the
  strategy. Size is the main trigger; document count, chunk similarity, and retrieval
  confidence are refinements to layer in later.

**Cosine/re-rank blend (not pure re-rank).**
- Pure cross-encoder re-ranking made ordering *noisier* on tiny pools (a generic header
  chunk jumped to #1). Blending `0.7*rerank + 0.3*cosine` (both min-max normalised) lets
  re-rank sharpen the order **without fully overriding** the vector signal. On small pools
  it can't do worse than cosine alone.

**Adaptive candidate count.** Never request more candidates than the KB holds
(`min(RETRIEVE_CANDIDATES, count())`).

**Multi-format loader registry + generalised locator.**
- The architecture already supported this: everything downstream works on `{text}`, so a
  new format is just a new loader returning the common record shape. `page` → `loc` because
  page is meaningless for slides/sheets. Tables (XLSX/CSV) are serialised row-by-row as
  `"Header: value; ..."` so they actually embed/retrieve (a raw grid is noise to an embedder).

**Web-search / "last resort" layers (design agreed, not built yet).**
- Layer 3 = open-source/web retrieval — **still grounded** (fetch real text, cite it). Layer 4
  = the model's own memory — the **true** last resort, labelled least-certain. Web search is
  **not** the last resort; it's the last *grounded* layer.
- Web fetch is **"just another retriever"**: reformulate query → fetch → chunk/embed → pick
  best passages (ephemeral, not stored). Triggered only when notes+KB come back weak
  (**CRAG-style retrieval evaluator**, source #2). Answers stay cited and marked lower-trust.
  Good web-grounded answers can **graduate into the KB**. Privacy: only the reformulated
  query leaves the machine — never the student's notes/personal data.

**Confidence gate (current state + intent).**
- Now: `weak = top_cosine < MIN_RETRIEVAL_SCORE (0.35)`, which only prints a ⚠. Observed
  separation is real: **0.615** for an answerable question vs **0.329 / 0.073** for two that
  should be refused.
- Next: make the gate **drive a real decision** — answer / honest-refuse / offer "ask a
  human". This single fork is the branch point that **both** the modes and the escalation
  path hang off. (Basis: Adaptive-RAG #3 + the hallucination survey #6.)

---

## 7. Config reference (`src/config.py`)

| Key | Value | Meaning |
|---|---|---|
| `CHUNK_SIZE` | 800 | approx chars per chunk |
| `CHUNK_OVERLAP` | 150 | chars shared between neighbouring chunks |
| `EMBED_MODEL` | all-MiniLM-L6-v2 | embedding model (swap point) |
| `EMBED_DIM` | 384 | vector size (must match embed model) |
| `TOP_K` | 5 | final chunks kept after re-rank (large-KB mode) |
| `COLLECTION_NAME` | vidhyasathi_kb | Qdrant collection |
| `QDRANT_PATH` | `<root>/qdrant_storage` | on-disk store |
| `OLLAMA_BASE_URL` | http://localhost:11434 | Ollama endpoint |
| `LLM_MODEL` | llama3.1:8b | answering model (swap point) |
| `MIN_RETRIEVAL_SCORE` | 0.35 | below this top cosine = "weak" (confidence gate) |
| `RETRIEVE_CANDIDATES` | 15 | wide candidate set before re-rank |
| `RERANK_MODEL` | ms-marco-MiniLM-L-6-v2 | cross-encoder (swap point) |
| `RERANK_WEIGHT` | 0.7 | blend: 0.7*rerank + 0.3*cosine |
| `SMALL_KB_MAX` | 30 | ≤ this many chunks ⇒ small-KB (whole-doc) mode |

Thresholds (`MIN_RETRIEVAL_SCORE`, `SMALL_KB_MAX`, `RERANK_WEIGHT`, chunk sizes) are
first-guess defaults to be **tuned against the eval set**, not sacred.

---

## 8. Conventions

- **All tunables in `config.py`.** No magic numbers elsewhere.
- **Model access behind wrappers** (`ask_llm`, `get_model`, `get_reranker`) → one-line swaps.
- **`build_index.py` is the only indexing entry point.** Other modules' `__main__` blocks
  are for stage-level debugging.
- **Re-embed after any change** to embedder, chunking, or documents (re-run `build_index`).
- Source documents live in `data/docs/` and are **git-ignored** (copyrighted texts stay local).

---

## 9. Gotchas / lessons already learned (don't re-discover these)

- **Python 3.11 f-strings can't contain a backslash inside `{ }`.** Compute the value
  (e.g. a `\u26a0` warning string) on a separate line, then interpolate the variable.
  (Allowed in 3.12+, so it's easy to trip on.)
- **Qdrant on-disk: always `close()`** the client when done — otherwise you get
  `Exception ignored in __del__ … Python is likely shutting down`. Also: **only one
  connection at a time** to the on-disk store.
- **`recreate_collection` is deprecated** → use `collection_exists` + `delete_collection`
  + `create_collection`.
- **PDF text extraction produces word-per-line artifacts** on justified/italic text →
  `clean()` collapses *all* whitespace to single spaces (done per-record, so document
  structure is preserved by the record boundaries, not by newlines).
- **Run as `python -m src.X`**, never `python src/X.py`, or the `from src....` imports break.
- Ollama's first `run`/`chat` after load lags a few seconds while the model loads into memory.

---

## 10. Verified behaviour (current test document)

Test doc: **"Data and Visual Analytics" syllabus (AIML0103)** — 4 pages, 16 chunks (small-KB mode).
- `python -m src.answer What visualization tools does the course cover?`
  → names Matplotlib/Seaborn/Plotly `[p2]`, Power BI/Tableau `[p3]`, with the full-context mode line.
- `python -m src.answer Who is the course instructor and what is their email`
  → **"I don't have that in the material."** (top score ~0.329 ⚠)
- `python -m src.answer What is the capital of France`
  → **"I don't have that in the material."** (top score ~0.073 ⚠) — refuses even though the
  model obviously *knows* it, because it isn't in the material. This is the whole thesis working.

---

## 11. Roadmap (ordered)

**Done:** Foundation spine (ingest→chunk→embed→store→retrieve→rerank→answer); size-aware
context; confidence gate v0 (flag only); adaptive candidate count; cosine/rerank blend;
**multi-format ingest** (pdf/docx/pptx/txt/md/csv/xlsx) with generalised locator citations.

**Next, in order:**
1. **Verify multi-format read path** — build KB from a real mixed set; confirm citations name
   the right source + locator; confirm the **large-KB branch** triggers (>30 chunks).
2. **Confidence gate drives behaviour** — answer / honest-refuse / offer "ask a human". This is
   the fork the modes + escalation branch from. *(Do this before the UI.)*
3. **Streamlit shell** — chat screen with the **mode dropdown**, showing sources + confidence
   per answer. The surface modes/escalation hang off.
4. **Teach Me mode** — system prompt + conversation memory over the same engine.
5. **Ask for Help / escalation** — doubt ticket → semantic helper matching → feedback into KB.
6. **Exam & Practice** — question generation, then OCR + vision grading (rubric feedback, not marks).

**Cross-cutting / later:**
- **Evaluation harness** — 30–50 Q test set + RAGChecker (drives every model-swap decision).
- **Web-retrieval layer** (Layer 3, CRAG-style) behind the same retriever interface.
- **Voice input** (faster-whisper).
- **Model upgrades** only when the eval set says which stage needs it (bigger LLM on the RTX 4060, stronger embedder, etc.).
- **MySQL** for profiles/tickets/logs; move Qdrant to a server if the KB outgrows on-disk.
- **Fill in the loader registry** for more formats; OCR for scanned PDFs/images.

---

## 12. Grounding literature (all 2024–2025; each maps to a system part)

1. **Agentic RAG: A Survey** — Singh et al., 2025 — arxiv 2501.09136 — direction/overview (modes, decisions)
2. **Corrective RAG (CRAG)** — Yan et al., 2024 — arxiv 2401.15884 — grounding correction / layer-3 fallback
3. **Adaptive-RAG** — Jeong et al., NAACL 2024 — arxiv 2403.14403 — when-to-retrieve / **confidence gate**
4. **From Local to Global: Graph RAG** — Edge et al., 2024 — arxiv 2404.16130 — scaling to whole-syllabus questions
5. **RAGChecker** — Ru et al., NeurIPS 2024 — arxiv 2408.08067 — **evaluation** (drives model swaps)
6. **Hallucination in LLMs: A Survey** — Huang et al., ACM TOIS 2024 — arxiv 2311.05232 — motivation/honesty
7. **LLMs for Education: A Survey** — Wang et al., 2024 — arxiv 2403.18105 — education context
8. **Short Answer Grading with RAG** — Chu et al., 2025 — arxiv 2504.05276 — the exam-grading feature

Deliverables already produced (decks/PDF): `Vidhyasathi-Review0` (16-slide zeroth-review),
`Vidhyasathi-Sources` (deck + PDF). Design palette/branding: teal "trust" palette, Cambria
headers + Calibri body, "Vidhyasathi" wordmark.

---

## 13. Name history

"Sage" was considered and **rejected**. The project is **Vidhyasathi** ("knowledge companion").
