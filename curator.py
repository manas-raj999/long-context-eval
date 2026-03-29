"""
Long-Context & Complex Reasoning Coding Evaluation Dataset
Curation Pipeline — GSoC 2026 / Gemini CLI

Author: Manas Raj (manas-raj999)
GitHub: https://github.com/manas-raj999
PRs: #21491, #21505

This pipeline:
1. Queries GitHub API to discover large, active, multi-language repos
2. Scores repos on complexity signals (not just stars/size)
3. Mines git history for PRs requiring true cross-component reasoning
4. Extracts and validates candidate tasks with difficulty tiering
5. Outputs a structured dataset schema ready for TestRig integration
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

# Target language pairs that create cross-boundary reasoning pressure
TARGET_LANGUAGE_COMBOS = [
    {"Python", "C"},
    {"Python", "C++"},
    {"TypeScript", "Go"},
    {"TypeScript", "Rust"},
    {"Java", "Kotlin"},
    {"Python", "Rust"},
]

# Repos known to have deep architectural complexity (seed set)
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
]


@dataclass
class RepoScore:
    repo: str
    stars: int
    size_kb: int
    languages: list[str]
    open_issues: int
    commit_frequency: float   # commits per week (approx)
    language_diversity: int   # number of distinct languages
    complexity_score: float   # composite 0-100
    rationale: str


@dataclass
class CandidateTask:
    repo: str
    base_commit: str
    pr_number: int
    pr_title: str
    problem_statement: str
    relevant_files: list[str]
    files_changed: int
    components_crossed: int       # estimated architectural boundary crossings
    min_context_tokens: int       # rough lower bound
    difficulty_tier: str          # "medium" | "hard" | "expert"
    failure_mode_category: str    # "retrieval" | "reasoning" | "cross_component"
    gold_patch_url: str
    language_pair: list[str]
    validation_status: str        # "pending" | "validated" | "rejected"
    notes: str = ""


def github_get(url: str, params: dict = None) -> dict | list | None:
    """Rate-limit-aware GET wrapper."""
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


def score_repo(repo_full_name: str) -> Optional[RepoScore]:
    """
    Score a repository on complexity signals relevant to long-context reasoning.

    Key insight: We're NOT optimizing for stars or size alone.
    We optimize for architectural depth — repos where understanding
    a bug requires tracing dependencies across subsystem boundaries.
    """
    data = github_get(f"{GITHUB_API}/repos/{repo_full_name}")
    if not data:
        return None

    # Get language breakdown
    langs_data = github_get(
        f"{GITHUB_API}/repos/{repo_full_name}/languages") or {}
    languages = list(langs_data.keys())

    # Estimate commit frequency from recent commits
    commits = github_get(
        f"{GITHUB_API}/repos/{repo_full_name}/commits",
        params={"per_page": 30, "since": "2024-01-01T00:00:00Z"}
    ) or []
    # Rough: 30 commits over ~60 weeks = 0.5/week
    commit_frequency = len(commits) / 60.0

    stars = data.get("stargazers_count", 0)
    size_kb = data.get("size", 0)
    open_issues = data.get("open_issues_count", 0)
    lang_diversity = len(languages)

    # Complexity scoring — weighted composite
    score = 0.0

    # Language diversity (cross-boundary reasoning pressure)
    # Max contribution: 30 points
    score += min(lang_diversity * 6, 30)

    # Is it a multi-language combo that creates API-boundary crossings?
    lang_set = set(languages[:5])
    for combo in TARGET_LANGUAGE_COMBOS:
        if combo.issubset(lang_set):
            score += 20
            break

    # Repo size (large = more context needed)
    # Max contribution: 20 points
    if size_kb > 500_000:
        score += 20
    elif size_kb > 100_000:
        score += 14
    elif size_kb > 50_000:
        score += 8

    # Active development (more real engineering problems)
    # Max contribution: 15 points
    score += min(commit_frequency * 10, 15)

    # Community scale (more meaningful issues/PRs)
    # Max contribution: 15 points
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
        repo=repo_full_name,
        stars=stars,
        size_kb=size_kb,
        languages=languages,
        open_issues=open_issues,
        commit_frequency=commit_frequency,
        language_diversity=lang_diversity,
        complexity_score=round(score, 1),
        rationale=rationale,
    )


def mine_cross_component_prs(repo_full_name: str, min_files: int = 5, max_prs: int = 50) -> list[CandidateTask]:
    """
    Mine merged PRs for cross-component reasoning tasks.

    Core filter logic:
    - PRs that touch >= min_files files
    - Files span multiple top-level directories (proxy for architectural boundaries)
    - NOT just test-only changes
    - Body/title suggests architectural dependency, not isolated fix

    This is the key insight: true long-context tasks require the agent to
    understand WHY changing X breaks Y, not just WHERE the change goes.
    """
    tasks = []

    # Fetch recent merged PRs
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

        # Skip trivial changes (docs, typos, version bumps)
        skip_keywords = ["bump", "typo", "readme",
                         "changelog", "version bump", "ci:", "docs:"]
        if any(kw in pr_title.lower() for kw in skip_keywords):
            continue

        # Get files changed in this PR
        files_data = github_get(
            f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/files",
            params={"per_page": 100}
        ) or []

        if len(files_data) < min_files:
            continue

        file_paths = [f["filename"] for f in files_data]

        # Skip if majority are test files
        test_files = sum(
            1 for f in file_paths if "test" in f.lower() or "spec" in f.lower())
        if test_files > len(file_paths) * 0.6:
            continue

        # Count architectural boundary crossings:
        # proxy = number of distinct top-level directories touched
        top_dirs = set()
        for fp in file_paths:
            parts = fp.split("/")
            if len(parts) > 1:
                top_dirs.add(parts[0])
        components_crossed = len(top_dirs)

        if components_crossed < 2:
            continue

        # Estimate context tokens (rough: avg 200 tokens per changed file)
        additions = sum(f.get("additions", 0) for f in files_data)
        deletions = sum(f.get("deletions", 0) for f in files_data)
        min_context_tokens = max(
            (additions + deletions) * 3, len(file_paths) * 200)

        # Difficulty tiering based on boundary crossings + context size
        if components_crossed >= 5 or min_context_tokens > 50_000:
            difficulty = "expert"
            failure_mode = "cross_component"
        elif components_crossed >= 3 or min_context_tokens > 20_000:
            difficulty = "hard"
            failure_mode = "reasoning"
        else:
            difficulty = "medium"
            failure_mode = "retrieval"

        # Build problem statement from PR title + body excerpt
        body_excerpt = (
            pr_body[:300] + "...") if len(pr_body) > 300 else pr_body
        problem_statement = f"{pr_title}\n\n{body_excerpt}".strip()

        base_commit = pr.get("base", {}).get("sha", "")[:12]

        tasks.append(CandidateTask(
            repo=repo_full_name,
            base_commit=base_commit,
            pr_number=pr_number,
            pr_title=pr_title,
            problem_statement=problem_statement,
            relevant_files=file_paths[:20],  # cap at 20 for schema size
            files_changed=len(file_paths),
            components_crossed=components_crossed,
            min_context_tokens=min_context_tokens,
            difficulty_tier=difficulty,
            failure_mode_category=failure_mode,
            gold_patch_url=f"https://github.com/{repo_full_name}/pull/{pr_number}/files",
            language_pair=[],
            validation_status="pending",
        ))

        # Respect rate limits — small sleep between PR file fetches
        time.sleep(0.3)

    return tasks


def run_pipeline(repos: list[str], output_dir: str = "dataset", min_score: float = 40.0):
    """
    Full curation pipeline:
    1. Score repos
    2. Filter by complexity threshold  
    3. Mine cross-component PRs
    4. Output structured dataset schema
    """
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Long-Context Eval Dataset — Curation Pipeline")
    print(f"  Repos to evaluate: {len(repos)}")
    print(f"  Complexity threshold: {min_score}")
    print(f"{'='*60}\n")

    scored_repos = []
    all_tasks = []

    # Phase 1: Score repos
    print("[Phase 1] Scoring repositories...")
    for repo in repos:
        print(f"  Scoring {repo}...")
        score = score_repo(repo)
        if score:
            scored_repos.append(score)
            print(
                f"    Score: {score.complexity_score:.1f} — {score.rationale}")
        time.sleep(0.5)

    # Filter by complexity threshold
    qualified = [r for r in scored_repos if r.complexity_score >= min_score]
    print(
        f"\n  Qualified repos: {len(qualified)}/{len(scored_repos)} (score >= {min_score})")

    # Save repo scores
    with open(f"{output_dir}/repo_scores.json", "w") as f:
        json.dump([asdict(r) for r in scored_repos], f, indent=2)
    print(f"  Saved repo scores → {output_dir}/repo_scores.json")

    # Phase 2: Mine tasks from qualified repos
    print(f"\n[Phase 2] Mining cross-component PRs...")
    for repo_score in qualified:
        print(f"\n  Mining {repo_score.repo}...")
        tasks = mine_cross_component_prs(
            repo_score.repo, min_files=5, max_prs=30)
        print(f"    Found {len(tasks)} candidate tasks")

        # Tag with language pair from repo
        for task in tasks:
            task.language_pair = repo_score.languages[:3]

        all_tasks.extend(tasks)
        time.sleep(1.0)

    # Phase 3: Output dataset schema
    print(f"\n[Phase 3] Building dataset schema...")

    # Summary stats
    by_difficulty = {}
    by_failure_mode = {}
    for t in all_tasks:
        by_difficulty[t.difficulty_tier] = by_difficulty.get(
            t.difficulty_tier, 0) + 1
        by_failure_mode[t.failure_mode_category] = by_failure_mode.get(
            t.failure_mode_category, 0) + 1

    dataset = {
        "metadata": {
            "version": "0.1.0-prototype",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_tasks": len(all_tasks),
            "qualified_repos": len(qualified),
            "difficulty_distribution": by_difficulty,
            "failure_mode_distribution": by_failure_mode,
            "schema_version": "long-context-eval-v1",
        },
        "tasks": [asdict(t) for t in all_tasks],
    }

    output_path = f"{output_dir}/candidate_tasks.json"
    with open(output_path, "w") as f:
        json.dump(dataset, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  Pipeline complete")
    print(f"  Total candidate tasks: {len(all_tasks)}")
    print(f"  Difficulty breakdown: {by_difficulty}")
    print(f"  Failure mode breakdown: {by_failure_mode}")
    print(f"  Output: {output_path}")
    print(f"{'='*60}\n")

    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Long-Context Eval Curation Pipeline")
    parser.add_argument("--repos", nargs="+", default=SEED_REPOS[:5],
                        help="List of repos (owner/name) to process")
    parser.add_argument("--output", default="dataset",
                        help="Output directory")
    parser.add_argument("--min-score", type=float, default=40.0,
                        help="Minimum complexity score (0-100)")
    args = parser.parse_args()

    run_pipeline(args.repos, args.output, args.min_score)
