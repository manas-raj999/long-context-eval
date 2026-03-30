"""
Long-Context & Complex Reasoning Coding Evaluation Dataset
Curation Pipeline v0.2 — GSoC 2026 / Gemini CLI

Upgrades over v0.1:
- Body-level PR filtering (catches doc PRs that slip through title filter)
- Global rename detection (filters mechanical text replacements)
- Token budget estimation (files_must_read_tokens, total_context_tokens, pressure_pct)
- Gold patch metadata on every task
- Contamination risk field based on PR date vs LLM training cutoffs
- Expanded seed set (15 repos)
"""

import os
import json
import time
import argparse
import requests
from dataclasses import dataclass, asdict, field
from typing import Optional
from datetime import datetime, timezone

GITHUB_API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
}

# Gemini 1M context window — for pressure calculation
GEMINI_CONTEXT_WINDOW = 1_000_000

# Known LLM training cutoffs (approximate) — for contamination risk
LLM_TRAINING_CUTOFFS = {
    "gpt4":    datetime(2023, 4,  1, tzinfo=timezone.utc),
    "gemini":  datetime(2024, 4,  1, tzinfo=timezone.utc),
    "claude3": datetime(2024, 8,  1, tzinfo=timezone.utc),
}

TARGET_LANGUAGE_COMBOS = [
    {"Python", "C"}, {"Python", "C++"}, {"TypeScript", "Go"},
    {"TypeScript", "Rust"}, {"Java", "Kotlin"}, {"Python", "Rust"},
    {"Go", "C"}, {"Ruby", "C"},
]

SEED_REPOS = [
    "django/django",
    "microsoft/vscode",
    "kubernetes/kubernetes",
    "rust-lang/rust",
    "python/cpython",
    "pytorch/pytorch",
    "langchain-ai/langchain",
    "apache/kafka",
    "golang/go",
    "redis/redis",
    "rails/rails",
    "fastapi/fastapi",
    "apache/airflow",
    "facebook/react",
    "torvalds/linux",
]

# Body-level skip keywords (catches doc PRs that slip through title filter)
BODY_SKIP_KEYWORDS = [
    "only documentation", "only docs", "doc fix", "typo fix",
    "spelling", "grammar fix", "update changelog", "release notes",
    "no code changes", "whitespace only",
]

# Title skip keywords
TITLE_SKIP_KEYWORDS = [
    "bump", "typo", "readme", "changelog", "version bump",
    "ci:", "docs:", "chore:", "style:", "release:", "revert:",
]


@dataclass
class RepoScore:
    repo: str
    stars: int
    size_kb: int
    languages: list
    open_issues: int
    commit_frequency: float
    language_diversity: int
    complexity_score: float
    rationale: str


@dataclass
class CandidateTask:
    repo: str
    base_commit: str
    pr_number: int
    pr_title: str
    problem_statement: str
    relevant_files: list
    files_changed: int
    components_crossed: int
    # Token budget fields
    files_must_read_tokens: int
    total_context_tokens: int
    pressure_pct: float          # % of Gemini 1M window
    difficulty_tier: str
    failure_mode_category: str
    # Gold patch
    gold_patch_url: str
    gold_patch_summary: str      # brief description of what the patch does
    # Contamination
    pr_merged_at: str
    contamination_risk: str      # "low" | "medium" | "high"
    contaminated_models: list    # which models likely saw this PR in training
    language_pair: list
    validation_status: str
    notes: str = ""


def github_get(url, params=None):
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 403:
            reset = int(resp.headers.get(
                "X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - int(time.time()), 5)
            print(f"  [rate limit] sleeping {wait}s...")
            time.sleep(wait)
            return github_get(url, params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [error] {url}: {e}")
        return None


def estimate_tokens(files_data):
    """
    Estimate token counts for a set of changed files.

    Rough heuristics:
    - Each line of code ≈ 10 tokens
    - additions + deletions as proxy for lines that must be read
    - total_context = must_read + estimated surrounding context (3x multiplier)
    """
    must_read_lines = sum(f.get("additions", 0) +
                          f.get("deletions", 0) for f in files_data)
    files_must_read_tokens = must_read_lines * 10

    # Surrounding context: each changed file contributes ~500 tokens of context
    context_tokens = len(files_data) * 500
    total_context_tokens = files_must_read_tokens + context_tokens

    pressure_pct = round(
        (total_context_tokens / GEMINI_CONTEXT_WINDOW) * 100, 2)

    return files_must_read_tokens, total_context_tokens, pressure_pct


def assess_contamination_risk(merged_at_str):
    """
    Assess contamination risk based on PR merge date vs LLM training cutoffs.
    Returns risk level and list of models that likely saw this PR.
    """
    if not merged_at_str:
        return "unknown", []

    try:
        merged_at = datetime.fromisoformat(
            merged_at_str.replace("Z", "+00:00"))
    except Exception:
        return "unknown", []

    contaminated = []
    for model, cutoff in LLM_TRAINING_CUTOFFS.items():
        if merged_at < cutoff:
            contaminated.append(model)

    if len(contaminated) >= 3:
        risk = "high"
    elif len(contaminated) >= 1:
        risk = "medium"
    else:
        risk = "low"

    return risk, contaminated


def is_global_rename(files_data, pr_title):
    """
    Detect if a PR is a mechanical global rename/refactor.

    Signals:
    - PR title contains rename/refactor keywords
    - Very high file count but low additions per file (uniform small changes)
    - Many files touched but changes are tiny and uniform
    """
    rename_keywords = ["rename", "refactor", "replace all", "global replace",
                       "lifetime illegal", "deprecated", "migration", "moved to"]

    title_lower = pr_title.lower()
    if any(kw in title_lower for kw in rename_keywords):
        if len(files_data) > 20:
            # Check if changes are suspiciously uniform (avg < 5 lines per file)
            total_changes = sum(f.get("additions", 0) +
                                f.get("deletions", 0) for f in files_data)
            avg_changes = total_changes / max(len(files_data), 1)
            if avg_changes < 8:
                return True
    return False


def score_repo(repo_full_name):
    data = github_get(f"{GITHUB_API}/repos/{repo_full_name}")
    if not data:
        return None

    langs_data = github_get(
        f"{GITHUB_API}/repos/{repo_full_name}/languages") or {}
    languages = list(langs_data.keys())

    commits = github_get(
        f"{GITHUB_API}/repos/{repo_full_name}/commits",
        params={"per_page": 30, "since": "2024-01-01T00:00:00Z"}
    ) or []
    commit_frequency = len(commits) / 60.0

    stars = data.get("stargazers_count", 0)
    size_kb = data.get("size", 0)
    open_issues = data.get("open_issues_count", 0)
    lang_diversity = len(languages)

    score = 0.0
    score += min(lang_diversity * 6, 30)

    lang_set = set(languages[:5])
    for combo in TARGET_LANGUAGE_COMBOS:
        if combo.issubset(lang_set):
            score += 20
            break

    if size_kb > 500_000:
        score += 20
    elif size_kb > 100_000:
        score += 14
    elif size_kb > 50_000:
        score += 8

    score += min(commit_frequency * 10, 15)

    if stars > 50_000:
        score += 15
    elif stars > 10_000:
        score += 10
    elif stars > 1_000:
        score += 5

    rationale = (
        f"{lang_diversity} languages ({', '.join(languages[:4])}), "
        f"{stars:,} stars, {size_kb:,} KB, "
        f"~{commit_frequency:.1f} commits/week"
    )

    return RepoScore(
        repo=repo_full_name, stars=stars, size_kb=size_kb,
        languages=languages, open_issues=open_issues,
        commit_frequency=commit_frequency, language_diversity=lang_diversity,
        complexity_score=round(score, 1), rationale=rationale,
    )


def mine_cross_component_prs(repo_full_name, min_files=5, max_prs=50):
    tasks = []

    prs = github_get(
        f"{GITHUB_API}/repos/{repo_full_name}/pulls",
        params={"state": "closed", "sort": "updated", "per_page": max_prs}
    ) or []

    for pr in prs:
        if not pr.get("merged_at"):
            continue

        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        pr_body = pr.get("body", "") or ""
        merged_at = pr.get("merged_at", "")

        # Title-level filter
        if any(kw in pr_title.lower() for kw in TITLE_SKIP_KEYWORDS):
            continue

        # Body-level filter (catches doc PRs that slip through title)
        if any(kw in pr_body.lower() for kw in BODY_SKIP_KEYWORDS):
            continue

        files_data = github_get(
            f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/files",
            params={"per_page": 100}
        ) or []

        if len(files_data) < min_files:
            continue

        file_paths = [f["filename"] for f in files_data]

        # Skip majority test files
        test_files = sum(
            1 for f in file_paths if "test" in f.lower() or "spec" in f.lower())
        if test_files > len(file_paths) * 0.6:
            continue

        # Global rename detection (v0.2 addition)
        if is_global_rename(files_data, pr_title):
            print(f"    [skip] global rename detected: {pr_title[:60]}")
            continue

        # Architectural boundary crossings
        top_dirs = set()
        for fp in file_paths:
            parts = fp.split("/")
            if len(parts) > 1:
                top_dirs.add(parts[0])
        components_crossed = len(top_dirs)

        if components_crossed < 2:
            continue

        # Token budget estimation (v0.2 addition)
        files_must_read_tokens, total_context_tokens, pressure_pct = estimate_tokens(
            files_data)

        # Contamination risk (v0.2 addition)
        contamination_risk, contaminated_models = assess_contamination_risk(
            merged_at)

        # Difficulty tiering
        if components_crossed >= 5 or pressure_pct > 5.0:
            difficulty = "expert"
            failure_mode = "cross_component"
        elif components_crossed >= 3 or pressure_pct > 2.0:
            difficulty = "hard"
            failure_mode = "reasoning"
        else:
            difficulty = "medium"
            failure_mode = "retrieval"

        body_excerpt = (
            pr_body[:400] + "...") if len(pr_body) > 400 else pr_body
        problem_statement = f"{pr_title}\n\n{body_excerpt}".strip()
        base_commit = pr.get("base", {}).get("sha", "")[:12]

        # Gold patch (v0.2 addition)
        gold_patch_url = f"https://github.com/{repo_full_name}/pull/{pr_number}/files"
        gold_patch_summary = (
            f"PR #{pr_number} modifies {len(file_paths)} files across "
            f"{components_crossed} top-level components. "
            f"Key files: {', '.join(file_paths[:3])}{'...' if len(file_paths) > 3 else ''}."
        )

        tasks.append(CandidateTask(
            repo=repo_full_name,
            base_commit=base_commit,
            pr_number=pr_number,
            pr_title=pr_title,
            problem_statement=problem_statement,
            relevant_files=file_paths[:20],
            files_changed=len(file_paths),
            components_crossed=components_crossed,
            files_must_read_tokens=files_must_read_tokens,
            total_context_tokens=total_context_tokens,
            pressure_pct=pressure_pct,
            difficulty_tier=difficulty,
            failure_mode_category=failure_mode,
            gold_patch_url=gold_patch_url,
            gold_patch_summary=gold_patch_summary,
            pr_merged_at=merged_at,
            contamination_risk=contamination_risk,
            contaminated_models=contaminated_models,
            language_pair=[],
            validation_status="pending",
        ))

        time.sleep(0.3)

    return tasks


def run_pipeline(repos, output_dir="dataset", min_score=40.0):
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Long-Context Eval Dataset — Curation Pipeline v0.2")
    print(f"  Repos to evaluate: {len(repos)}")
    print(f"  Complexity threshold: {min_score}")
    print(f"{'='*60}\n")

    scored_repos = []
    all_tasks = []

    print("[Phase 1] Scoring repositories...")
    for repo in repos:
        print(f"  Scoring {repo}...")
        score = score_repo(repo)
        if score:
            scored_repos.append(score)
            print(
                f"    Score: {score.complexity_score:.1f} — {score.rationale}")
        time.sleep(0.5)

    qualified = [r for r in scored_repos if r.complexity_score >= min_score]
    print(
        f"\n  Qualified repos: {len(qualified)}/{len(scored_repos)} (score >= {min_score})")

    with open(f"{output_dir}/repo_scores.json", "w") as f:
        json.dump([asdict(r) for r in scored_repos], f, indent=2)

    print(f"\n[Phase 2] Mining cross-component PRs...")
    for repo_score in qualified:
        print(f"\n  Mining {repo_score.repo}...")
        tasks = mine_cross_component_prs(
            repo_score.repo, min_files=5, max_prs=30)
        print(f"    Found {len(tasks)} candidate tasks")
        for task in tasks:
            task.language_pair = repo_score.languages[:3]
        all_tasks.extend(tasks)
        time.sleep(1.0)

    print(f"\n[Phase 3] Building dataset schema...")

    by_difficulty = {}
    by_failure_mode = {}
    by_contamination = {}
    pressure_buckets = {"<1%": 0, "1-5%": 0, "5-20%": 0, ">20%": 0}

    for t in all_tasks:
        by_difficulty[t.difficulty_tier] = by_difficulty.get(
            t.difficulty_tier, 0) + 1
        by_failure_mode[t.failure_mode_category] = by_failure_mode.get(
            t.failure_mode_category, 0) + 1
        by_contamination[t.contamination_risk] = by_contamination.get(
            t.contamination_risk, 0) + 1
        if t.pressure_pct < 1:
            pressure_buckets["<1%"] += 1
        elif t.pressure_pct < 5:
            pressure_buckets["1-5%"] += 1
        elif t.pressure_pct < 20:
            pressure_buckets["5-20%"] += 1
        else:
            pressure_buckets[">20%"] += 1

    dataset = {
        "metadata": {
            "version": "0.2.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_tasks": len(all_tasks),
            "qualified_repos": len(qualified),
            "difficulty_distribution": by_difficulty,
            "failure_mode_distribution": by_failure_mode,
            "contamination_distribution": by_contamination,
            "context_pressure_distribution": pressure_buckets,
            "schema_version": "long-context-eval-v2",
            "new_in_v2": [
                "body-level PR filtering",
                "global rename detection",
                "token_budget_estimate (files_must_read_tokens, total_context_tokens, pressure_pct)",
                "gold_patch_summary on every task",
                "contamination_risk field with model-level granularity",
            ]
        },
        "tasks": [asdict(t) for t in all_tasks],
    }

    output_path = f"{output_dir}/candidate_tasks.json"
    with open(output_path, "w") as f:
        json.dump(dataset, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  Pipeline v0.2 complete")
    print(f"  Total candidate tasks : {len(all_tasks)}")
    print(f"  Difficulty            : {by_difficulty}")
    print(f"  Failure mode          : {by_failure_mode}")
    print(f"  Contamination risk    : {by_contamination}")
    print(f"  Context pressure      : {pressure_buckets}")
    print(f"  Output                : {output_path}")
    print(f"{'='*60}\n")

    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Long-Context Eval Curation Pipeline v0.2")
    parser.add_argument("--repos", nargs="+", default=SEED_REPOS[:8],
                        help="List of repos (owner/name) to process")
    parser.add_argument("--output", default="dataset", help="Output directory")
    parser.add_argument("--min-score", type=float, default=40.0,
                        help="Minimum complexity score (0-100)")
    args = parser.parse_args()

    run_pipeline(args.repos, args.output, args.min_score)
