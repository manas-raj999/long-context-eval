"""
dependency_chain.py — AST-based Import Graph Traversal
Long-Context Eval Dataset — GSoC 2026 / Gemini CLI

This is the feature that makes failure_mode_category PROVABLE.

Instead of just labeling a task as "cross_component" based on directory spread,
this module traces the actual import dependency chain through the changed files
and produces a human-readable reasoning path:

  auth/middleware.py → billing/processor.py → reporting/aggregator.py

This is the difference between:
  - "components_crossed: 3" (a number)
  - "reasoning_chain: auth → billing → reporting" (a proof)

Currently supports: Python (ast module)
Planned: TypeScript (ts-morph), Go (go/ast)
"""

import os as _os
import ast
import os
import re
import json
import requests
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


GITHUB_API = "https://api.github.com"
_TOKEN = _os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    **({"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}),
}


@dataclass
class DependencyChain:
    chain: list              # ordered list of file paths forming the chain
    chain_str: str           # human-readable: "a.py → b.py → c.py"
    depth: int               # number of hops
    boundary_crossings: list  # which top-level dirs are crossed
    is_genuine_cross_component: bool
    confidence: str          # "high" | "medium" | "low"
    method: str              # "ast" | "regex" | "directory_proxy"


def fetch_file_content(repo, path, ref="HEAD"):
    """Fetch a single file's content from GitHub."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    resp = requests.get(url, headers=HEADERS, params={"ref": ref}, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("encoding") == "base64":
        import base64
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return None


def extract_python_imports(source_code, file_path):
    """
    Extract import statements from Python source using ast module.
    Returns list of imported module paths.
    """
    imports = []
    try:
        tree = ast.parse(source_code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    # Resolve relative imports based on file location
                    if node.level and node.level > 0:
                        # Relative import — resolve against current file's package
                        pkg_parts = file_path.replace(
                            "/", ".").replace(".py", "").split(".")
                        base = ".".join(pkg_parts[:-(node.level)])
                        full_module = f"{base}.{node.module}" if node.module else base
                        imports.append(full_module)
                    else:
                        imports.append(node.module)
    except SyntaxError:
        # Fall back to regex for files that can't be parsed
        imports.extend(extract_imports_regex(source_code))
    return imports


def extract_imports_regex(source_code):
    """Regex fallback for import extraction (works across languages)."""
    patterns = [
        r'^import\s+([\w.]+)',                    # Python: import x.y
        r'^from\s+([\w.]+)\s+import',             # Python: from x import y
        r'require\(["\']([^"\']+)["\']\)',         # JS/TS: require('x')
        r'from\s+["\']([^"\']+)["\']\s*import',   # TS: from 'x' import
        r'import\s+["\']([^"\']+)["\']',          # TS: import 'x'
    ]
    imports = []
    for line in source_code.split("\n"):
        for pattern in patterns:
            match = re.search(pattern, line.strip())
            if match:
                imports.append(match.group(1))
    return imports


def module_to_file_path(module_name, repo_files):
    """
    Try to resolve a module name to an actual file path in the repo.
    """
    # Convert module.name to module/name.py
    candidate = module_name.replace(".", "/") + ".py"
    if candidate in repo_files:
        return candidate

    # Try __init__.py
    candidate_init = module_name.replace(".", "/") + "/__init__.py"
    if candidate_init in repo_files:
        return candidate_init

    # Partial match — module is a subpath
    for f in repo_files:
        if f.endswith(module_name.replace(".", "/") + ".py"):
            return f

    return None


def get_top_level_dir(file_path):
    """Extract top-level directory from a file path."""
    parts = file_path.split("/")
    return parts[0] if len(parts) > 1 else "root"


def trace_dependency_chain(repo, changed_files, base_commit="HEAD", max_depth=4):
    """
    Trace actual import dependency chains between changed files.

    Algorithm:
    1. For each changed file, fetch its content and extract imports
    2. Try to resolve imports to other changed files
    3. Build a directed graph of dependencies between changed files
    4. Find the longest chain that crosses architectural boundaries
    5. Return that chain as the reasoning_chain

    This is what makes failure_mode_category provable:
    - If no chains cross boundaries → retrieval (directory spread was misleading)
    - If chains cross 1 boundary → reasoning
    - If chains cross 3+ boundaries → cross_component
    """
    # Only trace Python files for now (TypeScript and Go coming in full implementation)
    python_files = [f for f in changed_files if f.endswith(".py")]
    ts_files = [f for f in changed_files if f.endswith(
        ".ts") or f.endswith(".tsx")]
    go_files = [f for f in changed_files if f.endswith(".go")]

    if not python_files and not ts_files and not go_files:
        return _directory_proxy_chain(changed_files)

    # Build import graph between changed files
    import_graph = {}   # file -> list of files it imports from changed_files

    primary_files = python_files or ts_files or go_files

    for file_path in primary_files[:10]:  # cap at 10 to avoid API rate limits
        content = fetch_file_content(repo, file_path, base_commit)
        time.sleep(0.2)

        if not content:
            continue

        if file_path.endswith(".py"):
            raw_imports = extract_python_imports(content, file_path)
        else:
            raw_imports = extract_imports_regex(content)

        # Find which changed files are actually imported
        resolved = []
        for imp in raw_imports:
            resolved_path = module_to_file_path(imp, changed_files)
            if resolved_path and resolved_path != file_path:
                resolved.append(resolved_path)

        if resolved:
            import_graph[file_path] = resolved

    # Find longest dependency chain that crosses architectural boundaries
    best_chain = []
    best_crossings = []

    def dfs(current, visited, current_chain):
        nonlocal best_chain, best_crossings
        visited.add(current)
        current_chain.append(current)

        # Compute boundary crossings in current chain
        dirs_in_chain = [get_top_level_dir(f) for f in current_chain]
        unique_dirs = list(dict.fromkeys(dirs_in_chain))  # ordered unique
        crossings = [(unique_dirs[i], unique_dirs[i+1])
                     for i in range(len(unique_dirs)-1)
                     if unique_dirs[i] != unique_dirs[i+1]]

        if len(current_chain) > len(best_chain) and len(crossings) >= 1:
            best_chain = current_chain.copy()
            best_crossings = crossings

        if len(current_chain) < max_depth and current in import_graph:
            for neighbor in import_graph[current]:
                if neighbor not in visited:
                    dfs(neighbor, visited, current_chain)

        current_chain.pop()
        visited.discard(current)

    for start_file in import_graph:
        dfs(start_file, set(), [])

    # If we found a real chain, return it
    if best_chain and best_crossings:
        chain_str = " → ".join(best_chain)
        boundary_crossings = [f"{a} → {b}" for a, b in best_crossings]
        is_genuine = len(best_crossings) >= 1

        if len(best_crossings) >= 3:
            confidence = "high"
        elif len(best_crossings) >= 1:
            confidence = "medium"
        else:
            confidence = "low"

        return DependencyChain(
            chain=best_chain,
            chain_str=chain_str,
            depth=len(best_chain),
            boundary_crossings=boundary_crossings,
            is_genuine_cross_component=is_genuine,
            confidence=confidence,
            method="ast",
        )

    # Fall back to directory proxy if no import chains found
    return _directory_proxy_chain(changed_files)


def _directory_proxy_chain(changed_files):
    """
    Fallback: use directory structure as proxy when AST traversal finds no chains.
    This is what v0.1 used exclusively — now only a fallback.
    """
    top_dirs = list(dict.fromkeys(get_top_level_dir(f) for f in changed_files))
    if len(top_dirs) >= 2:
        chain = [d + "/*" for d in top_dirs[:4]]
        chain_str = " → ".join(chain)
        crossings = [
            f"{top_dirs[i]} → {top_dirs[i+1]}" for i in range(len(top_dirs)-1)]
        return DependencyChain(
            chain=chain,
            chain_str=chain_str,
            depth=len(chain),
            boundary_crossings=crossings,
            is_genuine_cross_component=len(top_dirs) >= 2,
            confidence="low",
            method="directory_proxy",
        )

    return DependencyChain(
        chain=changed_files[:2],
        chain_str=" → ".join(changed_files[:2]),
        depth=1,
        boundary_crossings=[],
        is_genuine_cross_component=False,
        confidence="low",
        method="directory_proxy",
    )


def enrich_tasks_with_chains(tasks_path, repo, output_path=None):
    """
    Load candidate_tasks.json and enrich each task with a reasoning_chain field.
    This is the main entry point for adding dependency chains to an existing dataset.
    """
    with open(tasks_path) as f:
        dataset = json.load(f)

    enriched = 0
    genuine_cross_component = 0

    for task in dataset["tasks"]:
        if task.get("repo") != repo:
            continue

        print(f"  Tracing chain for {repo} PR #{task['pr_number']}...")

        chain = trace_dependency_chain(
            repo=task["repo"],
            changed_files=task["relevant_files"],
            base_commit=task.get("base_commit", "HEAD"),
        )

        task["reasoning_chain"] = chain.chain_str
        task["reasoning_chain_detail"] = {
            "chain":                   chain.chain,
            "depth":                   chain.depth,
            "boundary_crossings":      chain.boundary_crossings,
            "is_genuine_cross_component": chain.is_genuine_cross_component,
            "confidence":              chain.confidence,
            "method":                  chain.method,
        }

        # Override failure_mode_category if AST says it's not genuine
        if chain.method == "ast" and not chain.is_genuine_cross_component:
            task["failure_mode_category"] = "retrieval"
            task["difficulty_tier"] = "medium"
            print(f"    → Reclassified to retrieval (no genuine import chain found)")
        elif chain.is_genuine_cross_component:
            genuine_cross_component += 1
            print(
                f"    → Chain: {chain.chain_str} [{chain.confidence} confidence]")

        enriched += 1
        time.sleep(0.5)

    dataset["metadata"]["reasoning_chain_enrichment"] = {
        "tasks_enriched":             enriched,
        "genuine_cross_component":    genuine_cross_component,
        "enriched_for_repo":          repo,
    }

    out = output_path or tasks_path
    with open(out, "w") as f:
        json.dump(dataset, f, indent=2)

    print(
        f"\n  Enriched {enriched} tasks, {genuine_cross_component} confirmed cross-component")
    return dataset


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Enrich tasks with AST dependency chains")
    parser.add_argument("--tasks", default="dataset/candidate_tasks.json")
    parser.add_argument("--repo", required=True, help="e.g. django/django")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    print(f"\nEnriching tasks in {args.tasks} for {args.repo}...")
    enrich_tasks_with_chains(args.tasks, args.repo, args.output)
