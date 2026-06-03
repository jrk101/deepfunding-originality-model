"""
Deep Funding Level II — GitHub Data Collector
Collects comprehensive repo metadata for all 98 repos to build originality features.
Handles rate limiting, retries, and fallbacks robustly.
"""

import os
import csv
import json
import time
import re
import base64
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
from dotenv import load_dotenv

# ─── CONFIG ──────────────────────────────────────────────────────────────────
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    print("ERROR: GITHUB_TOKEN not found in .env file")
    sys.exit(1)

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

BASE_URL = "https://api.github.com"
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "github_repo_data.csv"
RAW_JSON_DIR = OUTPUT_DIR / "raw_json"
RAW_JSON_DIR.mkdir(exist_ok=True)

# Rate limit safety
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
RATE_LIMIT_BUFFER = 100  # pause when remaining < this

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "collection.log", mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Dependency file patterns to look for
DEP_FILES = {
    "package.json": "npm",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "go.sum": "go",
    "requirements.txt": "pip",
    "setup.py": "pip",
    "setup.cfg": "pip",
    "pyproject.toml": "pip",
    "Pipfile": "pip",
    "Gemfile": "ruby",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "mix.exs": "elixir",
    "cabal.project": "haskell",
    "stack.yaml": "haskell",
    "CMakeLists.txt": "cmake",
    "Makefile": "make",
    "flake.nix": "nix",
    "default.nix": "nix",
}

# Keywords that suggest low originality (fork/wrapper)
FORK_WRAPPER_KEYWORDS = [
    "fork", "forked", "wrapper", "binding", "bindings", "bridge",
    "port", "ported", "clone", "based on", "built on top of",
    "thin wrapper", "sdk for", "client for",
]

# Keywords that suggest high originality
ORIGINALITY_KEYWORDS = [
    "from scratch", "original", "novel", "new approach", "reimplementation",
    "custom", "proprietary", "unique", "innovative", "ground up",
    "independent", "standalone",
]


# ─── API HELPERS ─────────────────────────────────────────────────────────────

def check_rate_limit():
    """Check GitHub API rate limit and wait if necessary."""
    try:
        resp = requests.get(f"{BASE_URL}/rate_limit", headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            remaining = data["resources"]["core"]["remaining"]
            reset_time = data["resources"]["core"]["reset"]
            if remaining < RATE_LIMIT_BUFFER:
                wait_seconds = max(reset_time - time.time(), 0) + 5
                logger.warning(f"Rate limit low ({remaining} remaining). Waiting {wait_seconds:.0f}s...")
                time.sleep(wait_seconds)
            return remaining
    except Exception as e:
        logger.warning(f"Rate limit check failed: {e}")
    return 9999  # assume ok


def api_get(url, params=None, accept_header=None):
    """Make a GET request to GitHub API with retries and rate limit handling."""
    headers = HEADERS.copy()
    if accept_header:
        headers["Accept"] = accept_header

    for attempt in range(MAX_RETRIES):
        try:
            check_rate_limit()
            resp = requests.get(url, headers=headers, params=params, timeout=30)

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 403:
                # Rate limited
                reset_time = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait_seconds = max(reset_time - time.time(), 0) + 5
                logger.warning(f"403 Rate limited. Waiting {wait_seconds:.0f}s (attempt {attempt + 1})")
                time.sleep(wait_seconds)
            elif resp.status_code == 404:
                logger.warning(f"404 Not Found: {url}")
                return None
            elif resp.status_code == 451:
                logger.warning(f"451 Unavailable for Legal Reasons: {url}")
                return None
            else:
                logger.warning(f"HTTP {resp.status_code} for {url} (attempt {attempt + 1})")
                time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout for {url} (attempt {attempt + 1})")
            time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error for {url} (attempt {attempt + 1})")
            time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            logger.error(f"Unexpected error for {url}: {e}")
            time.sleep(RETRY_DELAY)

    logger.error(f"All {MAX_RETRIES} attempts failed for {url}")
    return None


def api_get_paginated(url, params=None, max_pages=10):
    """Get paginated results from GitHub API."""
    all_items = []
    params = params or {}
    params["per_page"] = 100

    for page in range(1, max_pages + 1):
        params["page"] = page
        data = api_get(url, params=params)
        if data is None or len(data) == 0:
            break
        all_items.extend(data)
        if len(data) < 100:
            break  # last page

    return all_items


# ─── DATA COLLECTION FUNCTIONS ───────────────────────────────────────────────

def parse_repo_url(url):
    """Extract owner/repo from GitHub URL."""
    url = url.strip().rstrip("/")
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def get_repo_metadata(owner, repo):
    """Get core repo metadata."""
    data = api_get(f"{BASE_URL}/repos/{owner}/{repo}")
    if not data:
        return {}

    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "description": data.get("description", ""),
        "homepage": data.get("homepage", ""),
        "language": data.get("language", ""),
        "stars": data.get("stargazers_count", 0),
        "watchers": data.get("subscribers_count", 0),
        "forks_count": data.get("forks_count", 0),
        "open_issues": data.get("open_issues_count", 0),
        "size_kb": data.get("size", 0),
        "default_branch": data.get("default_branch", "main"),
        "is_fork": data.get("fork", False),
        "is_archived": data.get("archived", False),
        "is_template": data.get("is_template", False),
        "has_wiki": data.get("has_wiki", False),
        "has_pages": data.get("has_pages", False),
        "has_discussions": data.get("has_discussions", False),
        "license": data.get("license", {}).get("spdx_id", "") if data.get("license") else "",
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "pushed_at": data.get("pushed_at", ""),
        "topics": ",".join(data.get("topics", [])),
        "network_count": data.get("network_count", 0),
        "parent_repo": data.get("parent", {}).get("full_name", "") if data.get("parent") else "",
        "source_repo": data.get("source", {}).get("full_name", "") if data.get("source") else "",
    }


def get_contributor_count(owner, repo):
    """Get total number of contributors (up to 500 via pagination)."""
    # GitHub API returns max 500 contributors via list endpoint
    # Use a more efficient approach: check the contributors count via the stats
    contributors = api_get_paginated(
        f"{BASE_URL}/repos/{owner}/{repo}/contributors",
        params={"anon": "false"},
        max_pages=5,
    )
    if contributors is None:
        return 0
    return len(contributors)


def get_commit_stats(owner, repo):
    """Get commit statistics: total commits, recent activity."""
    # Get total commits from the default branch
    # Using the participation stats API for an efficient summary
    stats = api_get(f"{BASE_URL}/repos/{owner}/{repo}/stats/participation")

    total_commits_year = 0
    owner_commits_year = 0
    recent_weekly_avg = 0

    if stats and isinstance(stats, dict):
        all_commits = stats.get("all", [])
        owner_commits = stats.get("owner", [])
        total_commits_year = sum(all_commits) if all_commits else 0
        owner_commits_year = sum(owner_commits) if owner_commits else 0
        # Average of last 4 weeks
        if all_commits and len(all_commits) >= 4:
            recent_weekly_avg = sum(all_commits[-4:]) / 4

    # Also get the total commit count from the repo commits endpoint (first page gives total via Link header)
    try:
        resp = requests.get(
            f"{BASE_URL}/repos/{owner}/{repo}/commits",
            headers=HEADERS,
            params={"per_page": 1},
            timeout=15,
        )
        total_commits = 0
        if resp.status_code == 200:
            # Parse total from the Link header
            link_header = resp.headers.get("Link", "")
            match = re.search(r'page=(\d+)>; rel="last"', link_header)
            if match:
                total_commits = int(match.group(1))
            else:
                # Only one page of commits
                total_commits = len(resp.json())
    except Exception:
        total_commits = 0

    return {
        "total_commits": total_commits,
        "commits_last_year": total_commits_year,
        "owner_commits_year": owner_commits_year,
        "recent_weekly_commit_avg": round(recent_weekly_avg, 2),
    }


def get_language_breakdown(owner, repo):
    """Get language breakdown (bytes per language)."""
    data = api_get(f"{BASE_URL}/repos/{owner}/{repo}/languages")
    if not data:
        return {
            "languages_count": 0,
            "primary_language_pct": 0,
            "total_code_bytes": 0,
            "languages_detail": "",
        }

    total = sum(data.values()) if data else 1
    sorted_langs = sorted(data.items(), key=lambda x: x[1], reverse=True)
    primary_pct = round(sorted_langs[0][1] / total * 100, 1) if sorted_langs else 0

    return {
        "languages_count": len(data),
        "primary_language_pct": primary_pct,
        "total_code_bytes": total,
        "languages_detail": json.dumps(data),
    }


def get_release_info(owner, repo):
    """Get release statistics."""
    releases = api_get(f"{BASE_URL}/repos/{owner}/{repo}/releases", params={"per_page": 100})
    if not releases:
        return {"release_count": 0, "latest_release_date": "", "has_releases": False}

    return {
        "release_count": len(releases),
        "latest_release_date": releases[0].get("published_at", "") if releases else "",
        "has_releases": len(releases) > 0,
    }


def get_branch_count(owner, repo):
    """Get number of branches."""
    branches = api_get(f"{BASE_URL}/repos/{owner}/{repo}/branches", params={"per_page": 100})
    return len(branches) if branches else 0


def get_tag_count(owner, repo):
    """Get number of tags."""
    tags = api_get(f"{BASE_URL}/repos/{owner}/{repo}/tags", params={"per_page": 100})
    return len(tags) if tags else 0


def count_dependencies_in_content(content, dep_type):
    """Parse dependency count from file content."""
    try:
        if dep_type == "npm":
            # package.json
            data = json.loads(content)
            deps = len(data.get("dependencies", {}))
            dev_deps = len(data.get("devDependencies", {}))
            peer_deps = len(data.get("peerDependencies", {}))
            return deps, dev_deps + peer_deps
        elif dep_type == "cargo":
            # Cargo.toml - count [dependencies] entries
            in_deps = False
            in_dev_deps = False
            deps = 0
            dev_deps = 0
            for line in content.split("\n"):
                line = line.strip()
                if line == "[dependencies]":
                    in_deps = True
                    in_dev_deps = False
                    continue
                elif line in ("[dev-dependencies]", "[build-dependencies]"):
                    in_deps = False
                    in_dev_deps = True
                    continue
                elif line.startswith("["):
                    in_deps = False
                    in_dev_deps = False
                    continue
                if (in_deps or in_dev_deps) and "=" in line and not line.startswith("#"):
                    if in_deps:
                        deps += 1
                    else:
                        dev_deps += 1
            return deps, dev_deps
        elif dep_type == "go":
            # go.mod - count require lines
            deps = 0
            in_require = False
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("require ("):
                    in_require = True
                    continue
                elif line == ")" and in_require:
                    in_require = False
                    continue
                if in_require and line and not line.startswith("//"):
                    deps += 1
                elif line.startswith("require ") and not line.startswith("require ("):
                    deps += 1
            return deps, 0
        elif dep_type == "pip":
            if content.strip().startswith("{") or content.strip().startswith("["):
                # pyproject.toml as JSON-ish or actual JSON
                pass
            # requirements.txt style
            deps = 0
            for line in content.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    deps += 1
            return deps, 0
        elif dep_type == "maven":
            # pom.xml - count <dependency> tags
            deps = content.count("<dependency>")
            return deps, 0
        elif dep_type == "gradle":
            # build.gradle - count implementation/api/compile lines
            deps = 0
            for line in content.split("\n"):
                line = line.strip()
                if any(kw in line for kw in ["implementation ", "implementation(", "api ", "api(",
                                              "compile ", "compile(", "runtimeOnly", "compileOnly"]):
                    deps += 1
            return deps, 0
        elif dep_type == "elixir":
            # mix.exs - count {:dep_name, ...} in deps function
            deps = len(re.findall(r'\{:\w+,', content))
            return deps, 0
        elif dep_type == "haskell":
            # Look for build-depends in .cabal or dependencies in stack.yaml/cabal.project
            deps = len(re.findall(r'build-depends:', content))
            if deps == 0:
                deps = len(re.findall(r'- \w+', content))
            return max(deps, 0), 0
        else:
            return 0, 0
    except Exception as e:
        logger.debug(f"Error parsing {dep_type}: {e}")
        return 0, 0


def get_dependency_info(owner, repo, default_branch="main"):
    """Analyze dependency files in the repo root."""
    total_deps = 0
    total_dev_deps = 0
    dep_files_found = []
    dep_ecosystems = set()

    # Try to get root tree
    tree = api_get(f"{BASE_URL}/repos/{owner}/{repo}/git/trees/{default_branch}")
    if not tree or "tree" not in tree:
        # Fallback: try "master" branch
        tree = api_get(f"{BASE_URL}/repos/{owner}/{repo}/git/trees/master")

    if tree and "tree" in tree:
        root_files = {item["path"]: item for item in tree["tree"] if item["type"] == "blob"}

        for dep_file, dep_type in DEP_FILES.items():
            if dep_file in root_files:
                dep_files_found.append(dep_file)
                dep_ecosystems.add(dep_type)

                # Fetch the file content
                file_data = api_get(
                    f"{BASE_URL}/repos/{owner}/{repo}/contents/{dep_file}",
                    params={"ref": default_branch},
                )
                if file_data and "content" in file_data:
                    try:
                        content = base64.b64decode(file_data["content"]).decode("utf-8", errors="replace")
                        deps, dev_deps = count_dependencies_in_content(content, dep_type)
                        total_deps += deps
                        total_dev_deps += dev_deps
                    except Exception as e:
                        logger.debug(f"Error decoding {dep_file} for {owner}/{repo}: {e}")

    return {
        "dependency_count": total_deps,
        "dev_dependency_count": total_dev_deps,
        "dep_files_found": ",".join(dep_files_found),
        "dep_ecosystems": ",".join(dep_ecosystems),
        "dep_ecosystem_count": len(dep_ecosystems),
    }


def get_root_file_stats(owner, repo, default_branch="main"):
    """Get stats about root directory files."""
    tree = api_get(
        f"{BASE_URL}/repos/{owner}/{repo}/git/trees/{default_branch}",
        params={"recursive": "1"},
    )
    if not tree or "tree" not in tree:
        tree = api_get(
            f"{BASE_URL}/repos/{owner}/{repo}/git/trees/master",
            params={"recursive": "1"},
        )

    if not tree or "tree" not in tree:
        return {
            "total_files": 0,
            "total_dirs": 0,
            "tree_depth": 0,
            "has_ci_cd": False,
            "has_dockerfile": False,
            "has_docs": False,
            "has_tests": False,
            "has_examples": False,
            "has_benchmarks": False,
            "readme_size": 0,
            "src_file_count": 0,
            "test_file_count": 0,
            "config_file_count": 0,
        }

    items = tree.get("tree", [])
    files = [i for i in items if i["type"] == "blob"]
    dirs = [i for i in items if i["type"] == "tree"]

    # Compute tree depth
    max_depth = 0
    for item in items:
        depth = item["path"].count("/")
        max_depth = max(max_depth, depth)

    # Check for CI/CD
    ci_paths = [".github/workflows", ".circleci", ".travis.yml", "Jenkinsfile", ".gitlab-ci.yml"]
    has_ci_cd = any(
        any(item["path"].startswith(ci) or item["path"] == ci for item in items)
        for ci in ci_paths
    )

    has_dockerfile = any(
        "dockerfile" in item["path"].lower() or "docker-compose" in item["path"].lower()
        for item in items
    )

    has_docs = any(
        item["path"].lower().startswith("docs/") or item["path"].lower().startswith("doc/")
        for item in items
    )

    has_tests = any(
        "test" in item["path"].lower().split("/")[0]
        or item["path"].lower().startswith("tests/")
        or item["path"].lower().startswith("test/")
        or item["path"].lower().startswith("spec/")
        for item in items
    )

    has_examples = any(
        item["path"].lower().startswith("examples/") or item["path"].lower().startswith("example/")
        for item in items
    )

    has_benchmarks = any(
        "benchmark" in item["path"].lower() or "bench/" in item["path"].lower()
        for item in items
    )

    # README size
    readme_size = 0
    for item in files:
        if item["path"].lower().startswith("readme"):
            readme_size = item.get("size", 0)
            break

    # Source vs test vs config file counts
    src_extensions = {".py", ".js", ".ts", ".go", ".rs", ".sol", ".ex", ".hs",
                      ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb",
                      ".nim", ".vy", ".fe", ".jsx", ".tsx", ".mjs", ".cjs"}
    test_keywords = {"test", "spec", "_test", ".test.", ".spec."}
    config_extensions = {".yml", ".yaml", ".toml", ".json", ".ini", ".cfg", ".conf",
                         ".env", ".xml", ".lock"}

    src_count = 0
    test_count = 0
    config_count = 0
    for item in files:
        path_lower = item["path"].lower()
        ext = "." + path_lower.rsplit(".", 1)[-1] if "." in path_lower else ""
        if ext in src_extensions:
            if any(kw in path_lower for kw in test_keywords):
                test_count += 1
            else:
                src_count += 1
        elif ext in config_extensions:
            config_count += 1

    return {
        "total_files": len(files),
        "total_dirs": len(dirs),
        "tree_depth": max_depth,
        "has_ci_cd": has_ci_cd,
        "has_dockerfile": has_dockerfile,
        "has_docs": has_docs,
        "has_tests": has_tests,
        "has_examples": has_examples,
        "has_benchmarks": has_benchmarks,
        "readme_size": readme_size,
        "src_file_count": src_count,
        "test_file_count": test_count,
        "config_file_count": config_count,
    }


def analyze_description_and_topics(description, topics):
    """Analyze description and topics for originality signals."""
    text = f"{description} {topics}".lower()

    fork_wrapper_score = sum(1 for kw in FORK_WRAPPER_KEYWORDS if kw in text)
    originality_score = sum(1 for kw in ORIGINALITY_KEYWORDS if kw in text)

    return {
        "desc_fork_wrapper_signals": fork_wrapper_score,
        "desc_originality_signals": originality_score,
        "desc_length": len(description) if description else 0,
    }


def get_community_profile(owner, repo):
    """Get community profile metrics."""
    data = api_get(
        f"{BASE_URL}/repos/{owner}/{repo}/community/profile",
        accept_header="application/vnd.github.v3+json",
    )
    if not data:
        return {
            "health_percentage": 0,
            "has_code_of_conduct": False,
            "has_contributing": False,
            "has_issue_template": False,
            "has_pull_request_template": False,
        }

    files = data.get("files", {})
    return {
        "health_percentage": data.get("health_percentage", 0),
        "has_code_of_conduct": files.get("code_of_conduct") is not None,
        "has_contributing": files.get("contributing") is not None,
        "has_issue_template": files.get("issue_template") is not None,
        "has_pull_request_template": files.get("pull_request_template") is not None,
    }


def compute_derived_features(row):
    """Compute derived features from raw data."""
    # Age in days
    created = row.get("created_at", "")
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created_dt).days
        except Exception:
            age_days = 0
    else:
        age_days = 0

    # Days since last push
    pushed = row.get("pushed_at", "")
    if pushed:
        try:
            pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            days_since_push = (datetime.now(timezone.utc) - pushed_dt).days
        except Exception:
            days_since_push = 0
    else:
        days_since_push = 0

    stars = row.get("stars", 0) or 0
    forks = row.get("forks_count", 0) or 0
    size_kb = row.get("size_kb", 0) or 0
    deps = row.get("dependency_count", 0) or 0
    src_files = row.get("src_file_count", 0) or 0
    total_files = row.get("total_files", 0) or 0
    contributors = row.get("contributor_count", 0) or 0
    commits = row.get("total_commits", 0) or 0
    total_code_bytes = row.get("total_code_bytes", 0) or 0

    return {
        "age_days": age_days,
        "days_since_push": days_since_push,
        # Ratio features (with safe division)
        "fork_to_star_ratio": round(forks / max(stars, 1), 4),
        "deps_per_src_file": round(deps / max(src_files, 1), 4),
        "deps_per_1k_loc": round(deps / max(total_code_bytes / 1000, 1), 6),
        "src_to_total_file_ratio": round(src_files / max(total_files, 1), 4),
        "commits_per_contributor": round(commits / max(contributors, 1), 2),
        "stars_per_contributor": round(stars / max(contributors, 1), 2),
        "code_bytes_per_dep": round(total_code_bytes / max(deps, 1), 2),
        "commits_per_age_day": round(commits / max(age_days, 1), 4),
    }


# ─── MAIN COLLECTION ────────────────────────────────────────────────────────

def collect_repo_data(repo_url):
    """Collect all data for a single repo."""
    owner, repo = parse_repo_url(repo_url)
    if not owner or not repo:
        logger.error(f"Invalid repo URL: {repo_url}")
        return None

    logger.info(f"{'=' * 60}")
    logger.info(f"Collecting: {owner}/{repo}")

    # 1. Core metadata
    logger.info(f"  → Fetching metadata...")
    metadata = get_repo_metadata(owner, repo)
    if not metadata:
        logger.error(f"  ✗ Could not fetch metadata for {owner}/{repo}")
        return None

    default_branch = metadata.get("default_branch", "main")

    # 2. Contributors
    logger.info(f"  → Counting contributors...")
    contributor_count = get_contributor_count(owner, repo)

    # 3. Commit statistics
    logger.info(f"  → Fetching commit stats...")
    commit_stats = get_commit_stats(owner, repo)

    # 4. Language breakdown
    logger.info(f"  → Fetching language breakdown...")
    lang_info = get_language_breakdown(owner, repo)

    # 5. Release info
    logger.info(f"  → Fetching release info...")
    release_info = get_release_info(owner, repo)

    # 6. Branch and tag counts
    logger.info(f"  → Fetching branches/tags...")
    branch_count = get_branch_count(owner, repo)
    tag_count = get_tag_count(owner, repo)

    # 7. Dependency analysis
    logger.info(f"  → Analyzing dependencies...")
    dep_info = get_dependency_info(owner, repo, default_branch)

    # 8. File tree stats
    logger.info(f"  → Analyzing file tree...")
    file_stats = get_root_file_stats(owner, repo, default_branch)

    # 9. Description analysis
    desc_analysis = analyze_description_and_topics(
        metadata.get("description", ""),
        metadata.get("topics", ""),
    )

    # 10. Community profile
    logger.info(f"  → Fetching community profile...")
    community = get_community_profile(owner, repo)

    # Combine all data
    row = {"repo_url": repo_url, "owner": owner, "repo_name": repo}
    row.update(metadata)
    row["contributor_count"] = contributor_count
    row.update(commit_stats)
    row.update(lang_info)
    row.update(release_info)
    row["branch_count"] = branch_count
    row["tag_count"] = tag_count
    row.update(dep_info)
    row.update(file_stats)
    row.update(desc_analysis)
    row.update(community)

    # 11. Derived features
    derived = compute_derived_features(row)
    row.update(derived)

    logger.info(f"  ✓ Done! Stars={row['stars']}, Contributors={contributor_count}, "
                f"Deps={dep_info['dependency_count']}, Files={file_stats['total_files']}, "
                f"IsFork={metadata['is_fork']}")

    # Save raw JSON
    json_path = RAW_JSON_DIR / f"{owner}__{repo}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        # Convert bools for JSON
        json_safe = {}
        for k, v in row.items():
            if isinstance(v, bool):
                json_safe[k] = v
            else:
                json_safe[k] = v
        json.dump(json_safe, f, indent=2, default=str)

    return row


def main():
    """Main collection pipeline."""
    logger.info("=" * 70)
    logger.info("DEEP FUNDING LEVEL II — GitHub Data Collection")
    logger.info("=" * 70)

    # Load repos
    repos_df = pd.read_csv(Path(__file__).parent / "repos_to_predict.csv")
    repo_urls = repos_df["repo"].dropna().tolist()
    logger.info(f"Found {len(repo_urls)} repos to collect data for.")

    # Check rate limit
    remaining = check_rate_limit()
    logger.info(f"GitHub API rate limit remaining: {remaining}")

    # Collect data for each repo
    all_data = []
    failed_repos = []

    for i, url in enumerate(repo_urls, 1):
        logger.info(f"\n[{i}/{len(repo_urls)}] Processing: {url}")
        try:
            row = collect_repo_data(url)
            if row:
                all_data.append(row)
            else:
                failed_repos.append(url)
                logger.warning(f"  ✗ No data collected for {url}")
        except Exception as e:
            logger.error(f"  ✗ Unexpected error for {url}: {e}")
            failed_repos.append(url)

        # Progress checkpoint - save every 10 repos
        if i % 10 == 0:
            logger.info(f"\n{'─' * 40}")
            logger.info(f"CHECKPOINT: {i}/{len(repo_urls)} repos processed, {len(all_data)} successful, {len(failed_repos)} failed")
            # Save intermediate results
            if all_data:
                df = pd.DataFrame(all_data)
                df.to_csv(OUTPUT_FILE, index=False)
                logger.info(f"Intermediate results saved to {OUTPUT_FILE}")
            logger.info(f"{'─' * 40}\n")

    # Retry failed repos once
    if failed_repos:
        logger.info(f"\nRetrying {len(failed_repos)} failed repos...")
        for url in failed_repos.copy():
            logger.info(f"  Retrying: {url}")
            time.sleep(2)
            try:
                row = collect_repo_data(url)
                if row:
                    all_data.append(row)
                    failed_repos.remove(url)
            except Exception as e:
                logger.error(f"  ✗ Retry also failed for {url}: {e}")

    # Save final results
    if all_data:
        df = pd.DataFrame(all_data)
        df.to_csv(OUTPUT_FILE, index=False)
        logger.info(f"\n{'=' * 70}")
        logger.info(f"COLLECTION COMPLETE")
        logger.info(f"  Total repos: {len(repo_urls)}")
        logger.info(f"  Successful:  {len(all_data)}")
        logger.info(f"  Failed:      {len(failed_repos)}")
        logger.info(f"  Output:      {OUTPUT_FILE}")
        logger.info(f"  Raw JSON:    {RAW_JSON_DIR}")
        logger.info(f"{'=' * 70}")

        if failed_repos:
            logger.warning(f"\nFailed repos:")
            for url in failed_repos:
                logger.warning(f"  - {url}")

        # Print summary stats
        logger.info(f"\n{'─' * 40}")
        logger.info(f"DATA QUALITY SUMMARY")
        logger.info(f"{'─' * 40}")
        numeric_cols = df.select_dtypes(include=["number"]).columns
        for col in numeric_cols:
            zeros = (df[col] == 0).sum()
            nulls = df[col].isnull().sum()
            if zeros > 0 or nulls > 0:
                logger.info(f"  {col}: {zeros} zeros, {nulls} nulls (of {len(df)})")
    else:
        logger.error("No data collected!")

    return all_data


if __name__ == "__main__":
    main()
