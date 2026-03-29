# Long-Context & Complex Reasoning Coding Evaluation Dataset
### GSoC 2026 Prototype — Gemini CLI

> **Status:** Active prototype — curation pipeline v0.1  
> Part of my GSoC 2026 proposal for [google-gemini/gemini-cli issue #23316](https://github.com/google-gemini/gemini-cli/issues/23316)

---

## The Core Problem

Existing benchmarks like SWE-bench and TerminalBench are **saturating** — agents score well because most tasks are *retrieval disguised as multi-file*. The agent finds the right function, patches it locally, and passes.

A true long-context reasoning task is different:

> Changing an API contract in `auth/middleware.ts` breaks an invariant assumed by `billing/processor.py`, which propagates to `reporting/aggregator.go`.

The agent must trace **architectural dependency chains** — not just retrieve a file.

This pipeline's primary filter is **verified cross-component boundary crossings**, not file count or token size.

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
          - composite score 0-100
    │
    ▼
[Phase 2] Mine cross-component PRs
          - Filter: >= 5 files changed
          - Filter: spans >= 2 top-level directories (boundary proxy)
          - Filter: not test-only, not docs/bump
          - Tag: difficulty tier + failure_mode_category
    │
    ▼
[Phase 3] Output structured dataset schema
          - candidate_tasks.json (TestRig-compatible schema)
          - repo_scores.json (reproducible curation audit trail)
```

---

## Dataset Schema

Each task in `candidate_tasks.json` follows this schema:

```json
{
  "repo": "django/django",
  "base_commit": "a3f2c1d9",
  "pr_number": 18432,
  "pr_title": "Fix ORM query cross-database backend propagation",
  "problem_statement": "...",
  "relevant_files": ["django/db/backends/base/", "django/db/models/sql/", "..."],
  "files_changed": 12,
  "components_crossed": 4,
  "min_context_tokens": 32000,
  "difficulty_tier": "hard",
  "failure_mode_category": "cross_component",
  "gold_patch_url": "https://github.com/django/django/pull/18432/files",
  "language_pair": ["Python", "C", "JavaScript"],
  "validation_status": "pending"
}
```

### `failure_mode_category` — the key field

This field is what makes the baseline report actionable, not just a pass/fail number:

| Category | Meaning |
|---|---|
| `retrieval` | Task looks multi-file but agent can solve by finding one function |
| `reasoning` | Agent must understand why a change causes a downstream effect |
| `cross_component` | Agent must trace through 3+ architectural boundaries to solve |

---

## Quickstart

```bash
# Install
pip install requests

# Set your GitHub token (recommended — 5000 req/hr vs 60)
export GITHUB_TOKEN=your_token_here

# Run on default seed repos (5 repos, quick test)
python curator.py

# Run on custom repos with lower threshold
python curator.py --repos django/django microsoft/vscode --min-score 35

# Full seed set (takes longer due to API rate limits)
python curator.py --repos django/django microsoft/vscode kubernetes/kubernetes \
  rust-lang/rust pytorch/pytorch --output dataset --min-score 40
```

---

## Planned Next Steps (Full GSoC Scope)

- [ ] Static import graph analysis (AST-based, not just directory proxy)
- [ ] Docker-per-task containerization for reproducible evaluation
- [ ] `TestRig` / `evalTest` integration for nightly pipeline
- [ ] Human validation pass on difficulty tiers
- [ ] 30-50 repo target with 200+ validated tasks
- [ ] Baseline analysis: run Gemini CLI and categorize failure modes

---

## Related Contributions to gemini-cli

- [#21491](https://github.com/google-gemini/gemini-cli/pull/21491) — `refactor(sdk): replace console.error with injectable AgentLogger interface`
- [#21505](https://github.com/google-gemini/gemini-cli/pull/21505) — `docs(sdk): add JSDoc to exported interfaces in packages/sdk/src/types.ts`

---

*Manas Raj · [github.com/manas-raj999](https://github.com/manas-raj999)*
