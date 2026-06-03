"""
Deep Funding Level II — Cargo.lock Dependency Counter
=======================================================
For Rust workspace repos that returned 0 deps from both GitHub parser 
and deps.dev, we fetch the Cargo.lock file directly from raw GitHub.

Cargo.lock contains ALL transitive dependencies (the full resolved dep tree).
Counting [[package]] entries gives us the total transitive dep count.

This is the most reliable signal for Rust projects.

Run: python collect_cargo_lock.py
Output: data/cargo_lock_data.csv
No API key needed — fetches from raw.githubusercontent.com
"""

import csv
import json
import re
import time
import logging
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ── CONFIG ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "cargo_lock_data.csv"
SLEEP = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "cargo_lock.log", mode="w"),
    ],
)
logger = logging.getLogger(__name__)

# ── REPO LIST ─────────────────────────────────────────────────────────────────
# All Rust repos that showed 0 deps from both GitHub parser and deps.dev
# We try both 'main' and 'master' branches

RUST_REPOS = [
    # (github_url, [branch_candidates])
    ("https://github.com/paradigmxyz/reth",                 ["main"]),
    ("https://github.com/foundry-rs/foundry",               ["main"]),
    ("https://github.com/alloy-rs/alloy",                   ["main"]),
    ("https://github.com/succinctlabs/sp1",                 ["main", "dev"]),
    ("https://github.com/0xMiden/miden-vm",                 ["next", "main"]),
    ("https://github.com/grandinetech/grandine",            ["main"]),
    ("https://github.com/Cyfrin/aderyn",                    ["main", "dev"]),
    ("https://github.com/Commit-Boost/commit-boost-client", ["main"]),
    ("https://github.com/succinctlabs/op-succinct",         ["main"]),
    ("https://github.com/EspressoSystems/jellyfish",        ["main"]),
    ("https://github.com/flashbots/rbuilder",               ["develop", "main"]),
    ("https://github.com/OffchainLabs/stylus-sdk-rs",       ["main", "stylus"]),
    ("https://github.com/risc0/risc0-ethereum",             ["main", "release-1.0"]),
    ("https://github.com/succinctlabs/rsp",                 ["main"]),
    ("https://github.com/powdr-labs/powdr",                 ["main"]),
    ("https://github.com/axiom-crypto/snark-verifier",      ["main"]),
    ("https://github.com/lambdaclass/ethrex",               ["main"]),
    ("https://github.com/lambdaclass/lambdaworks",          ["main"]),  # has some data but let's get full count
    ("https://github.com/sigp/lighthouse",                  ["stable", "main"]),
    ("https://github.com/Plonky3/Plonky3",                  ["main"]),
    ("https://github.com/arkworks-rs/algebra",              ["master", "main"]),
    ("https://github.com/edb-rs/edb",                       ["main"]),
    ("https://github.com/argotorg/fe",                      ["master", "main"]),
    # Also grab some that had data to cross-validate
    ("https://github.com/a16z/helios",                      ["master", "main"]),
    ("https://github.com/erigontech/silkworm",              ["master", "main"]),  # C++ but has Makefile
]

# Also Go repos that had 0 from deps.dev (go.mod line count was used from GitHub)
# We'll also check go.sum for a transitive count
GO_REPOS = [
    ("https://github.com/ethpandaops/checkpointz",          ["master", "main"]),
    ("https://github.com/ethereum/go-ethereum",             ["master", "main"]),
    ("https://github.com/erigontech/erigon",                ["main"]),
    ("https://github.com/OffchainLabs/prysm",               ["develop", "main"]),
    ("https://github.com/NethermindEth/juno",               ["main"]),
    ("https://github.com/Consensys/gnark-crypto",           ["master", "main"]),
    ("https://github.com/flashbots/mev-boost-relay",        ["main"]),
    ("https://github.com/flashbots/mev-boost",              ["main"]),
    ("https://github.com/wealdtech/ethdo",                  ["master", "main"]),
    ("https://github.com/holiman/goevmlab",                 ["master", "main"]),
    ("https://github.com/taikoxyz/taiko-mono",              ["main"]),
    ("https://github.com/supranational/blst",               ["master"]),
]


# ── HELPERS ───────────────────────────────────────────────────────────────────

def fetch_raw(url: str) -> str | None:
    """Fetch raw text from a URL."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"HTTP {e.code}: {url}")
        return None
    except (URLError, Exception) as e:
        logger.warning(f"Error fetching {url}: {e}")
        return None


def count_cargo_lock_packages(content: str) -> dict:
    """
    Count packages in Cargo.lock.
    Each [[package]] entry is one dependency (including the root package itself).
    We subtract 1 to exclude the root package if present.
    """
    # Count [[package]] headers
    package_count = content.count("[[package]]")

    # Also extract names for analysis
    names = re.findall(r'^name = "(.+)"', content, re.MULTILINE)

    # Count unique external deps (not workspace members)
    # We can't easily distinguish workspace vs external, so we use total - 1
    total_pkgs = package_count
    estimated_external = max(0, total_pkgs - 1)  # subtract root

    return {
        "cargo_lock_total_packages": total_pkgs,
        "cargo_lock_external_estimate": estimated_external,
        "cargo_lock_found": True,
    }


def count_go_sum_deps(content: str) -> dict:
    """
    Count unique modules in go.sum.
    go.sum has two entries per module version (hash + h1 hash), so divide by ~2.
    Each line: module version hash
    """
    lines = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith("//")]
    # Get unique module@version combinations
    modules = set()
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            modules.add(parts[0])  # module@version

    # go.sum entries include /go.mod hashes too, so count unique base modules
    base_modules = set()
    for m in modules:
        # Remove /go.mod suffix versions
        base = m.split("/go.mod")[0] if "/go.mod" in m else m
        base_modules.add(base)

    return {
        "go_sum_unique_modules": len(base_modules),
        "go_sum_found": True,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def process_rust_repo(github_url: str, branches: list) -> dict:
    owner_repo = github_url.replace("https://github.com/", "")
    result = {
        "repo_url": github_url,
        "lock_type": "Cargo.lock",
        "cargo_lock_found": False,
        "cargo_lock_total_packages": 0,
        "cargo_lock_external_estimate": 0,
        "go_sum_found": False,
        "go_sum_unique_modules": 0,
        "branch_used": "",
    }

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/Cargo.lock"
        logger.info(f"  Trying: {url}")
        time.sleep(SLEEP)
        content = fetch_raw(url)

        if content and "[[package]]" in content:
            counts = count_cargo_lock_packages(content)
            result.update(counts)
            result["branch_used"] = branch
            logger.info(f"  ✓ Found Cargo.lock on branch '{branch}': {counts['cargo_lock_total_packages']} packages")
            return result
        else:
            logger.info(f"  ✗ Not found on branch '{branch}'")

    logger.warning(f"  ✗ Cargo.lock not found for {owner_repo}")
    return result


def process_go_repo(github_url: str, branches: list) -> dict:
    owner_repo = github_url.replace("https://github.com/", "")
    result = {
        "repo_url": github_url,
        "lock_type": "go.sum",
        "cargo_lock_found": False,
        "cargo_lock_total_packages": 0,
        "cargo_lock_external_estimate": 0,
        "go_sum_found": False,
        "go_sum_unique_modules": 0,
        "branch_used": "",
    }

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/go.sum"
        logger.info(f"  Trying: {url}")
        time.sleep(SLEEP)
        content = fetch_raw(url)

        if content and len(content) > 100:
            counts = count_go_sum_deps(content)
            result.update(counts)
            result["branch_used"] = branch
            logger.info(f"  ✓ Found go.sum on branch '{branch}': {counts['go_sum_unique_modules']} modules")
            return result
        else:
            logger.info(f"  ✗ Not found on branch '{branch}'")

    logger.warning(f"  ✗ go.sum not found for {owner_repo}")
    return result


def save(results):
    if not results:
        return
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)


def main():
    logger.info("=" * 70)
    logger.info("DEEP FUNDING — Cargo.lock / go.sum Dependency Counter")
    logger.info("=" * 70)

    results = []
    total = len(RUST_REPOS) + len(GO_REPOS)

    logger.info(f"\nProcessing {len(RUST_REPOS)} Rust repos (Cargo.lock)...")
    for i, (url, branches) in enumerate(RUST_REPOS, 1):
        name = url.split("/")[-1]
        logger.info(f"\n[{i}/{len(RUST_REPOS)}] {name}")
        r = process_rust_repo(url, branches)
        results.append(r)

    logger.info(f"\nProcessing {len(GO_REPOS)} Go repos (go.sum)...")
    for i, (url, branches) in enumerate(GO_REPOS, 1):
        name = url.split("/")[-1]
        logger.info(f"\n[{i}/{len(GO_REPOS)}] {name}")
        r = process_go_repo(url, branches)
        results.append(r)

    save(results)

    # Summary
    rust_found = sum(1 for r in results if r["cargo_lock_found"])
    go_found = sum(1 for r in results if r["go_sum_found"])
    logger.info(f"\n{'='*70}")
    logger.info(f"DONE — Cargo.lock found: {rust_found}/{len(RUST_REPOS)} | go.sum found: {go_found}/{len(GO_REPOS)}")
    logger.info(f"Output: {OUTPUT_FILE}")

    logger.info(f"\n{'Repo':<35} {'Type':<12} {'Pkgs/Modules':>14}")
    logger.info("-" * 65)
    for r in results:
        name = r["repo_url"].split("/")[-1]
        if r["cargo_lock_found"]:
            logger.info(f"{name:<35} {'Cargo.lock':<12} {r['cargo_lock_total_packages']:>14}")
        elif r["go_sum_found"]:
            logger.info(f"{name:<35} {'go.sum':<12} {r['go_sum_unique_modules']:>14}")
        else:
            logger.info(f"{name:<35} {'NOT FOUND':<12} {'N/A':>14}")


if __name__ == "__main__":
    main()
