# Long-Context & Complex Reasoning Coding Evaluation Dataset
### GSoC 2026 Prototype — Gemini CLI

> **Status:** Active prototype — curation pipeline v0.2  
> Part of my GSoC 2026 proposal for [google-gemini/gemini-cli issue #23316](https://github.com/google-gemini/gemini-cli/issues/23316)  
> Proposal submitted on the GSoC portal — March 31, 2026

---

## The Core Problem

Benchmarks like SWE-bench and TerminalBench are saturating — not because agents are getting good at complex engineering, but because most tasks are **retrieval disguised as multi-file**. The agent finds the right function, patches it locally, and passes.

A true long-context reasoning task is different:

> Changing an API contract in `auth/middleware.ts` breaks an invariant assumed by `billing/processor.py`, which propagates to `reporting/aggregator.go`.

The agent must trace **architectural dependency chains** — not just retrieve a file.

This pipeline's primary filter is **verified cross-component boundary crossings**, not file count or token size.

---

## Current State (v0.2)

| Metric | Value |
|--------|-------|
| Repos qualified | 10 |
| Candidate tasks | 41 |
| Expert / Hard / Medium | 4 / 18 / 19 |
| Cross-component / Reasoning / Retrieval | 4 / 18 / 19 |
| Tasks with `reasoning_chain` | 25 |
| Context pressure >5% of Gemini 1M window | 4 tasks |
| Contamination risk field | All 41 tasks |

**Repos:** django, microsoft/vscode, kubernetes, rust-lang/rust, python/cpython, pytorch, langchain-ai/langchain, golang/go, rails, fastapi

---

## What This Pipeline Does

```
GitHub API
    │
    ▼
[Phase 1] Score repos on complexity signals
          - language diversity (cross-boundary pressure)
          - multi-language combos (Python+C, TS+Go, etc.)
          - repo size + commit activity
          - composite score 0–100
    │
    ▼
[Phase 2] Mine cross-component PRs
          - Filter: >= 5 files changed
          - Filter: spans >= 2 top-level directories
          - Filter: not test-only, not docs/bump (body-level)
          - Filter: not global rename/refactor (v0.2)
          - Tag: difficulty tier + failure_mode_category
    │
    ▼
[Phase 3] Output structured dataset schema
          - candidate_tasks.json (TestRig-compatible schema)
          - repo_scores.json (reproducible curation audit trail)
    │
    ▼
[Phase 4] AST dependency chain enrichment (dependency_chain.py)
          - Traces actual import paths between changed files
          - Adds reasoning_chain field per task
          - Reclassifies tasks where no genuine import chain exists
```

---

## Dataset Schema (v2)

Each task in `candidate_tasks.json`:

```json
{
  "repo": "pytorch/pytorch",
  "base_commit": "a3f2c1d9",
  "pr_number": 113,
  "pr_title": "Add Conv2d support to nn.modules",
  "problem_statement": "...",
  "relevant_files": [
    "torch/nn/modules/__init__.py",
    "torch/nn/modules/conv.py",
    "test/test_torch.py"
  ],
  "files_changed": 8,
  "components_crossed": 3,
  "files_must_read_tokens": 1200,
  "total_context_tokens": 5200,
  "pressure_pct": 0.52,
  "difficulty_tier": "hard",
  "failure_mode_category": "reasoning",
  "gold_patch_url": "https://github.com/pytorch/pytorch/pull/113/files",
  "gold_patch_summary": "PR #113 modifies 8 files across 3 top-level components...",
  "reasoning_chain": "torch/nn/modules/__init__.py → torch/nn/modules/conv.py → test/test_torch.py",
  "reasoning_chain_detail": {
    "chain": ["torch/nn/modules/__init__.py", "torch/nn/modules/conv.py", "test/test_torch.py"],
    "depth": 3,
    "boundary_crossings": ["torch/nn/modules → test"],
    "is_genuine_cross_component": true,
    "confidence": "medium",
    "method": "ast"
  },
  "pr_merged_at": "2017-09-14T10:23:00Z",
  "contamination_risk": "high",
  "contaminated_models": ["gpt4", "gemini", "claude3"],
  "language_pair": ["Python", "C++", "Cuda"],
  "validation_status": "pending"
}
```

### Schema fields

| Field | Description |
|-------|-------------|
| `failure_mode_category` | `retrieval` / `reasoning` / `cross_component` — makes baseline report actionable |
| `reasoning_chain` | Traced import dependency path: `a.py → b.py → c.py` |
| `pressure_pct` | Context window pressure as % of Gemini's 1M token limit |
| `contamination_risk` | `low` / `medium` / `high` — based on PR date vs LLM training cutoffs |
| `contaminated_models` | Which models likely saw this PR during training |
| `gold_patch_url` | Direct link to PR diff — ground truth for evaluation |

### `failure_mode_category` — why it matters

Instead of "agent passed 34% of tasks", the baseline report can say:
> "agent handles `retrieval` tasks at 71% but `cross_component` at 8% — the bottleneck is architectural reasoning, not context length."

| Category | Meaning | Agent failure pattern |
|----------|---------|----------------------|
| `retrieval` | Task solvable by finding one function | Agent passes — false positive |
| `reasoning` | Requires understanding cause-effect across modules | Agent breaks downstream invariant |
| `cross_component` | Requires tracing 3+ architectural boundaries | Agent can't identify all affected components |

---

## What's New in v0.2

- **Body-level PR filtering** — catches doc PRs that slip through title-only filters
- **Global rename detection** — filters mechanical text replacements (e.g. Rust #10897)
- **Token budget estimation** — `files_must_read_tokens`, `total_context_tokens`, `pressure_pct`
- **Gold patch metadata** — `gold_patch_url` + `gold_patch_summary` on every task
- **Contamination risk** — per-task, per-model granularity using PR merge date vs training cutoffs
- **`dependency_chain.py`** — AST-based import graph traversal, `reasoning_chain` field per task

---

## Quickstart

```bash
# Install
pip install requests

# Set your GitHub token (5000 req/hr vs 60 unauthenticated)
export GITHUB_TOKEN=your_token_here

# Run on default seed repos
python curator.py

# Run full seed set
python curator.py \
  --repos django/django microsoft/vscode kubernetes/kubernetes \
  rust-lang/rust python/cpython pytorch/pytorch \
  langchain-ai/langchain golang/go rails/rails fastapi/fastapi \
  --output dataset --min-score 40

# Enrich tasks with AST dependency chains
python dependency_chain.py --tasks dataset/candidate_tasks.json --repo pytorch/pytorch
python dependency_chain.py --tasks dataset/candidate_tasks.json --repo langchain-ai/langchain
python dependency_chain.py --tasks dataset/candidate_tasks.json --repo django/django
```

---

## Planned Next Steps (Full GSoC Scope)

- [ ] AST traversal for TypeScript (ts-morph) and Rust (syn crate)
- [ ] Docker-per-task containerization for reproducible evaluation
- [ ] TestRig / evalTest integration for nightly pipeline
- [ ] Human validation pass on difficulty tiers
- [ ] 30–50 repo target with 200+ validated tasks
- [ ] Baseline analysis: run Gemini CLI and categorize failure modes by `failure_mode_category`
- [ ] Contamination-aware train/test splits using temporal metadata

---

## Related Contributions to gemini-cli

- [#21491](https://github.com/google-gemini/gemini-cli/pull/21491) — `refactor(sdk): replace console.error with injectable AgentLogger interface`
- [#21505](https://github.com/google-gemini/gemini-cli/pull/21505) — `docs(sdk): add JSDoc to exported interfaces in packages/sdk/src/types.ts`

---

*Manas Raj · [github.com/manas-raj999](https://github.com/manas-raj999)*
