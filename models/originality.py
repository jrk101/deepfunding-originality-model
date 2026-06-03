import csv, json, logging, sys, io, itertools
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import numpy as np

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
DATA_DIR        = BASE_DIR / "data"
FORMULA_OUT     = DATA_DIR / "formula_scores.csv"
SUBMISSION_OUT  = DATA_DIR / "submission.csv"

GITHUB_FILE     = DATA_DIR / "github_repo_data.csv"
DEPSDEV_FILE    = DATA_DIR / "deps_dev_data.csv"
CARGO_FILE      = DATA_DIR / "cargo_lock_data.csv"
README_FILE     = DATA_DIR / "readme_data.csv"
AI_SIGNALS_FILE = DATA_DIR / "ai_signals.csv"
DEP_GRAPH_FILE  = DATA_DIR / "seedReposWithDependencies.json"
NO_TRANS_FILE   = DATA_DIR / "seedReposWithNoTransitiveDependencies.json"
USAGE_SCORES_FILE = DATA_DIR / "usage_scores.csv"
PUBLIC_JURY_FILE  = DATA_DIR / "originalityPublic.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "formula.log", mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def _repo_age_years(created_at_str: str) -> float:
    """Parse created_at ISO string → age in years from today. Returns 1.0 on failure."""
    try:
        if not created_at_str or str(created_at_str).lower() in ("nan", "none", ""):
            return 1.0
        s = str(created_at_str).strip().replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).days / 365.25
        return max(age, 0.1)
    except Exception:
        return 1.0


def load_data() -> pd.DataFrame:
    log.info("Loading data sources...")
    github  = pd.read_csv(GITHUB_FILE)
    depsdev = pd.read_csv(DEPSDEV_FILE)
    cargo   = pd.read_csv(CARGO_FILE)

    df = github.merge(depsdev, on="repo_url", how="left")
    df = df.merge(
        cargo[["repo_url", "cargo_lock_total_packages", "go_sum_unique_modules"]],
        on="repo_url", how="left"
    )
    for col in ["cargo_lock_total_packages", "go_sum_unique_modules",
                "depsdev_direct_deps", "depsdev_transitive_deps"]:
        df[col] = df[col].fillna(0)

    # ── Repo age from created_at ──────────────────────────────────────────────
    created_col = None
    for c in df.columns:
        if "created" in c.lower():
            created_col = c
            break
    if created_col:
        df["repo_age_years"] = df[created_col].apply(_repo_age_years)
        log.info(f"  Repo age: min={df['repo_age_years'].min():.1f}y  "
                 f"max={df['repo_age_years'].max():.1f}y  "
                 f"mean={df['repo_age_years'].mean():.1f}y  (from '{created_col}')")
    else:
        df["repo_age_years"] = 3.0
        log.warning("  created_at column not found — repo_age_years set to 3.0")

    # ── star_score ────────────────────────────────────────────────────────────
    _max_log_stars = np.log1p(df["stars"].max())
    df["star_score"] = np.log1p(df["stars"].fillna(0)) / _max_log_stars

    # ── activity_score ────────────────────────────────────────────────────────
    age_years     = df["repo_age_years"].clip(lower=0.1)
    commits_yr    = df["commits_last_year"].fillna(0).clip(lower=0)
    activity_raw  = np.log1p(age_years * commits_yr)
    _max_activity = activity_raw.max()
    df["activity_score"] = activity_raw / _max_activity if _max_activity > 0 else 0.0

    # ── Full dep graph ────────────────────────────────────────────────────────
    dep_graph_raw    = {}
    dep_graph_counts = {}
    if DEP_GRAPH_FILE.exists():
        with open(DEP_GRAPH_FILE, "r", encoding="utf-8") as _f:
            dep_graph_raw = json.load(_f)
        dep_graph_counts = {
            url.replace("https://github.com/", "").lower(): len(deps)
            for url, deps in dep_graph_raw.items()
        }
        log.info(f"  Dep graph: {len(dep_graph_counts)} repos, "
                 f"{sum(dep_graph_counts.values())} edges")
    else:
        log.warning("  seedReposWithDependencies.json not found")

    df["graph_dep_count"] = df.apply(
        lambda r: dep_graph_counts.get(
            str(r.get("repo_url","")).replace("https://github.com/","").lower(), 0),
        axis=1)

    def best_dep(r):
        if r["graph_dep_count"]           > 0: return r["graph_dep_count"]
        if r["cargo_lock_total_packages"] > 0: return r["cargo_lock_total_packages"]
        if r["go_sum_unique_modules"]     > 0: return r["go_sum_unique_modules"]
        if r["depsdev_transitive_deps"]   > 0: return r["depsdev_transitive_deps"]
        return r.get("dependency_count", 0)

    df["unified_dep_count"] = df.apply(best_dep, axis=1)

    def scale_dep(r):
        raw = r["unified_dep_count"]
        if r["cargo_lock_total_packages"] > 0: return raw * 0.30
        if r["go_sum_unique_modules"]     > 0: return raw * 0.40
        eco = str(r.get("depsdev_ecosystem", "") or "").upper()
        if "MAVEN" in eco:                     return raw * 0.80
        return raw

    PROXIES = {
        "libBLS": 50, "mcl": 50, "blst": 20, "nimbus-eth2": 400,
        "DAppNode": 5, "simple-optimism-node": 3,
        "ethereum-helm-charts": 5, "ethereum-package": 5, "libp2p": 0,
    }
    for name, proxy in PROXIES.items():
        mask = (df["repo_name"] == name) & (df["unified_dep_count"] == 0)
        df.loc[mask, "unified_dep_count"] = proxy

    df["scaled_dep_count"] = df.apply(scale_dep, axis=1)

    # ── Age-adjusted dep count ─────────────────────────────────────────────
    # Older repos accumulate deps over time — penalise them less per dep.
    # adj_deps = scaled_deps / log1p(age_years)
    # v44: cap age at 7 years — prevents runaway inflation for very old repos.
    # geth (12y) and trueblocks-core (9y) both get the 7-year benefit, not more.
    age_capped = df["repo_age_years"].clip(upper=7.0)
    df["age_adj_dep_count"] = df["scaled_dep_count"] / np.log1p(age_capped)
    log.info(f"  age_adj_dep: mean={df['age_adj_dep_count'].mean():.1f}  "
             f"max={df['age_adj_dep_count'].max():.1f}")

    n_with = (df["unified_dep_count"] > 0).sum()
    log.info(f"  {len(df)} repos | {n_with} with dep signal")

    # ── direct_ratio ──────────────────────────────────────────────────────────
    direct_counts = {}
    if NO_TRANS_FILE.exists():
        with open(NO_TRANS_FILE, "r", encoding="utf-8") as _f:
            no_trans_raw = json.load(_f)
        direct_counts = {
            url.replace("https://github.com/", "").lower(): len(deps)
            for url, deps in no_trans_raw.items()
        }

    def get_direct_ratio(row) -> float:
        url   = str(row.get("repo_url","")).replace("https://github.com/","").lower()
        total = row["unified_dep_count"]
        if total == 0: return 1.0
        direct = direct_counts.get(url, float(row.get("depsdev_direct_deps", 0) or 0))
        return float(np.clip(direct / total, 0.0, 1.0))

    df["direct_ratio"] = df.apply(get_direct_ratio, axis=1)

    # ── dep_quality ───────────────────────────────────────────────────────────
    def get_dep_quality(row) -> float:
        url     = str(row.get("repo_url","")).replace("https://github.com/","").lower()
        my_deps = dep_graph_raw.get(f"https://github.com/{url}", [])
        if not my_deps: return 0.0
        avg = float(np.mean([
            dep_graph_counts.get(d.replace("https://github.com/","").lower(), 0)
            for d in my_deps
        ]))
        return float(np.clip(np.log1p(avg) / np.log1p(500), 0.0, 1.0))

    df["dep_quality"] = df.apply(get_dep_quality, axis=1)

    # ── usage_signal ──────────────────────────────────────────────────────────
    df["usage_signal"] = 0.0
    if USAGE_SCORES_FILE.exists():
        try:
            usage = pd.read_csv(USAGE_SCORES_FILE)
            def _norm_url(s):
                s = str(s).strip().rstrip("/")
                return s if s.startswith("https://") else f"https://github.com/{s}"
            usage["repo_url_norm"] = usage["repo"].apply(_norm_url)
            grp         = usage.groupby("repo_url_norm")
            n_deps      = grp["dependency"].count()
            n_source    = grp["source_match"].sum()
            total_w     = grp["weighted_score"].sum()
            source_ratio = (n_source / n_deps.clip(lower=1)).clip(0, 1)
            avg_w        = total_w / n_deps.clip(lower=1)
            intensity    = (np.log1p(avg_w) / np.log1p(avg_w.max() + 1e-9)).clip(0, 1)
            signal_map   = (0.5 * source_ratio + 0.5 * intensity).to_dict()
            df["usage_signal"] = df["repo_url"].map(signal_map).fillna(0.0)
            log.info(f"  usage_signal: {(df['usage_signal']>0).sum()}/{len(df)} repos  "
                     f"mean={df['usage_signal'].mean():.3f}")
        except Exception as e:
            log.warning(f"  usage_scores.csv skipped: {e}")

    # ── README signal ─────────────────────────────────────────────────────────
    README_HIGH = [
        "bls12-381","stark","snark","plonk","polynomial","commitment scheme",
        "zero-knowledge","zk","cryptographic","signature library","elliptic curve",
        "field arithmetic","prime field","zkvm","proving","verifier","circuit",
        "compiler","language","specification language","formal","type system",
        "bytecode","evm bytecode","smart contract language",
        "execution client","consensus client","ethereum protocol","beacon chain",
        "ethereum node","full node","implementation of","virtual machine",
        "rollup","layer 2","proof system","assembly",
    ]
    README_LOW = [
        "docker","helm","kubernetes","yaml","configuration","deployment",
        "run ethereum","node setup","easy to","simple way","quick start",
        "toolkit","scaffold","template","boilerplate","starter kit",
        "getting started","built with","powered by",
        "list of","set of","collection of","registry","chain id",
    ]
    df["readme_tech_signal"] = 0
    if README_FILE.exists():
        try:
            rdm = pd.read_csv(README_FILE)
            def _rscore(text):
                t = str(text).lower()
                return int(np.clip(
                    sum(1 for kw in README_HIGH if kw in t) -
                    sum(1 for kw in README_LOW  if kw in t),
                    -3, 5))
            rdm["_sig"] = rdm["readme_excerpt"].fillna("").apply(_rscore)
            sig_map = dict(zip(rdm["repo_url"], rdm["_sig"]))
            df["readme_tech_signal"] = df["repo_url"].map(sig_map).fillna(0).astype(int)
        except Exception as e:
            log.warning(f"  README signal skipped: {e}")

    # ── AI signals ────────────────────────────────────────────────────────────
    for col in ["ai_scratch","ai_protocol","ai_novel","ai_wrapper",
                "ai_found","ai_template","ai_glue","ai_utility"]:
        df[col] = 0.0
    if AI_SIGNALS_FILE.exists():
        try:
            ais = pd.read_csv(AI_SIGNALS_FILE)
            col_map = {
                "ai_scratch":  "implements_from_scratch",
                "ai_protocol": "defines_new_protocol",
                "ai_novel":    "has_novel_algorithms",
                "ai_wrapper":  "is_wrapper_or_integration",
                "ai_found":    "is_foundational_infrastructure",
                "ai_template": "is_template_or_scaffold",
                "ai_glue":     "is_glue_code_or_middleware",
                "ai_utility":  "is_utility_library_only",
            }
            for dest, src in col_map.items():
                if src in ais.columns:
                    sm = dict(zip(ais["repo_url"],
                                  pd.to_numeric(ais[src], errors="coerce").fillna(0)))
                    df[dest] = df["repo_url"].map(sm).fillna(0)
            log.info(f"  AI signals: {len(ais)} repos")
        except Exception as e:
            log.warning(f"  AI signals skipped: {e}")

    return df


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2a — WEIGHTED PRODUCER / CONSUMER SIGNALS
# ══════════════════════════════════════════════════════════════════════════════
#
# Two-pass weighted producer scoring:
#   Pass 1: raw inbound count → initial producer_score_raw (0-1)
#   Pass 2: re-weight each inbound edge by the depending repo's raw producer score
#           so being depended on by geth/alloy/blst matters more than
#           being depended on by a config/tooling repo.
#
# consumer_score: outbound age-adjusted dep count, log-normalised
# pc_ratio: producer / (consumer + eps), normalised

def compute_producer_consumer_signals(df: pd.DataFrame) -> pd.DataFrame:
    if not DEP_GRAPH_FILE.exists():
        df["producer_score"] = 0.0
        df["consumer_score"] = 0.0
        df["pc_ratio"]       = 0.5
        log.info("  Producer/consumer: dep graph missing — set to defaults.")
        return df

    with open(DEP_GRAPH_FILE, "r", encoding="utf-8") as _f:
        dep_graph_raw = json.load(_f)

    def slug(url):
        return url.lower().rstrip("/").replace("https://github.com/", "")

    our_slugs = {slug(u) for u in df["repo_url"]}
    slug_to_idx = {slug(u): i for i, u in enumerate(df["repo_url"])}

    # Build inbound adjacency: target_slug → list of source_slugs
    inbound: dict[str, list] = {s: [] for s in our_slugs}
    for src_url, dep_urls in dep_graph_raw.items():
        src_s = slug(src_url)
        if src_s not in our_slugs:
            continue
        for dep_url in dep_urls:
            dep_s = slug(dep_url)
            if dep_s in inbound:
                inbound[dep_s].append(src_s)

    # Pass 1: raw inbound count → normalised with sqrt compression
    # sqrt spreads the distribution — prevents a few repos dominating at 1.0
    # while everything else collapses to 0.
    raw_inbound = np.array([len(inbound.get(slug(u), [])) for u in df["repo_url"]], dtype=float)
    max_raw  = raw_inbound.max()
    prod_raw = np.sqrt(raw_inbound / (max_raw + 1e-9))   # 0-1, compressed

    # Pass 2: weighted inbound — each edge weighted by source's pass-1 score
    weighted_inbound = np.zeros(len(df), dtype=float)
    for i, url in enumerate(df["repo_url"]):
        s = slug(url)
        for src_s in inbound.get(s, []):
            src_idx = slug_to_idx.get(src_s)
            if src_idx is not None:
                weighted_inbound[i] += prod_raw[src_idx]

    # v43: sqrt compression on final producer score too — avoids spike distribution
    max_wi   = weighted_inbound.max()
    producer = np.sqrt(weighted_inbound / (max_wi + 1e-9))

    # consumer_score: age-adjusted outbound dep count
    outbound = df["age_adj_dep_count"].values.astype(float)
    max_out  = np.log1p(outbound.max())
    consumer = np.log1p(outbound) / (max_out + 1e-9)

    # pc_ratio
    pc_raw   = producer / (consumer + 0.1)
    pc_ratio = pc_raw / (pc_raw.max() + 1e-9)

    df["producer_score"] = np.round(producer, 4)
    df["consumer_score"] = np.round(consumer, 4)
    df["pc_ratio"]       = np.round(pc_ratio, 4)

    top = df.nlargest(8, "producer_score")[["repo_url","producer_score","consumer_score","pc_ratio"]]
    log.info("  Top producers (weighted 2-pass):")
    for _, r in top.iterrows():
        log.info(f"    {r['repo_url'].split('/')[-1]:<28} "
                 f"prod={r['producer_score']:.3f}  "
                 f"cons={r['consumer_score']:.3f}  "
                 f"pc={r['pc_ratio']:.3f}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2b — CATEGORY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

_CATEGORY_RULES = [
    ("zk_system",         +0.06, [
        "zkvm","zk vm","zero-knowledge","zk proof","proving system","proof system",
        "recursive proof","snark","stark","plonk","zkp toolkit","plonky",
        "snark verifier","zk verifier","groth16","succinct proof",
    ], []),
    ("compiler_language", +0.06, [
        "smart contract language","programming language for","language for ethereum",
        "evm language","solidity compiler","vyper compiler",
        "formal verification language","specification language","certora",
    ], []),
    ("execution_client",  +0.05, [
        "execution client","ethereum client","ethereum protocol",
        "c++ implementation of the ethereum","rust implementation of the ethereum",
        "implementation of the ethereum protocol","ethereum execution layer",
        "ethereum node","evm-compatible","full ethereum node",
    ], ["Go","Rust","C++","C#","Java","Kotlin","Elixir"]),
    ("consensus_client",  +0.05, [
        "consensus client","beacon client","beacon chain client","beacon node",
        "consensus layer","ethereum consensus","eth2 client","proof-of-stake client",
    ], []),
    ("crypto_primitive",  +0.05, [
        "bls12","pairing-based","elliptic curve library","cryptographic library",
        "crypto library","bls signatures","assembly bls",
        "pure javascript cryptography","pure typescript cryptography",
        "python elliptic","python ecc","javascript cryptography","typescript cryptography",
    ], []),
    ("smart_contract",    +0.02, [
        "smart contract library","solidity library","gas-optimized","gas optimized",
        "account abstraction","safe contracts","multisig","openzeppelin",
    ], []),
    ("spec_standard",     +0.03, [
        "specification","ethereum improvement proposal","execution api","consensus spec",
        "smart contract debugging data format","networking specification",
        "libp2p spec","api spec",
    ], []),
    ("dev_tooling",       +0.01, [
        "development framework","testing framework","smart contract development",
        "solidity development","ethereum development","web3 development",
        "block explorer","blockchain explorer","static analysis","linter","formatter",
        "debugger","symbolic execution","fuzzer","wallet library","ethereum library",
        "deployment tool","hardhat plugin","block builder","mev relay","mev boost",
        "staking tool","validator tool","deposit cli","abi","contract verification",
        "source verification","checkpoint sync",
    ], []),
    ("config_infra",      -0.04, [
        "helm chart","docker-compose","docker setup","kubernetes","kurtosis",
        "docker configuration","node setup","run ethereum","running ethereum",
        "set of helm","multiple components","dappnode",
    ], []),
    ("data_repo",         -0.06, [
        "list of evm","list of chains","community-maintained list",
        "crowdsourced","chain metadata","chainlist","network information",
    ], []),
]

# v42: overrides contain ONLY category labels — bonus always 0.0
# Category bonus comes from _CATEGORY_RULES above (now much smaller)
_REPO_CATEGORY_OVERRIDES = {
    "erigon":("execution_client",0.0), "besu":("execution_client",0.0),
    "evmone":("execution_client",0.0), "ethrex":("execution_client",0.0),
    "helios":("execution_client",0.0), "juno":("execution_client",0.0),
    "taiko-mono":("execution_client",0.0),
    "nimbus-eth2":("consensus_client",0.0), "prysm":("consensus_client",0.0),
    "lambda_ethereum_consensus":("consensus_client",0.0),
    "solidity":("compiler_language",0.0),
    "Plonky3":("zk_system",0.0), "rsp":("zk_system",0.0),
    "op-succinct":("zk_system",0.0), "snark-verifier":("zk_system",0.0),
    "lambdaworks":("zk_system",0.0),
    "noble-curves":("crypto_primitive",0.0), "py_ecc":("crypto_primitive",0.0),
    "js-ethereum-cryptography":("crypto_primitive",0.0),
    "bls":("crypto_primitive",0.0), "gnark-crypto":("crypto_primitive",0.0),
    "algebra":("crypto_primitive",0.0),
    "openzeppelin-contracts":("smart_contract",0.0),
    "safe-smart-account":("smart_contract",0.0), "solady":("smart_contract",0.0),
    "solidity-lib":("smart_contract",0.0), "account-abstraction":("smart_contract",0.0),
    "execution-apis":("spec_standard",0.0), "libp2p":("spec_standard",0.0),
    "hevm":("dev_tooling",0.0), "halmos":("dev_tooling",0.0),
    "foundry":("dev_tooling",0.0), "hardhat":("dev_tooling",0.0),
    "alloy":("dev_tooling",0.0), "ethers.js":("dev_tooling",0.0),
    "sourcify":("dev_tooling",0.0), "titanoboa":("dev_tooling",0.0),
    "edb":("dev_tooling",0.0), "goevmlab":("dev_tooling",0.0),
    "aderyn":("dev_tooling",0.0), "intellij-solidity":("dev_tooling",0.0),
    "mev-boost":("dev_tooling",0.0), "mev-boost-relay":("dev_tooling",0.0),
    "rbuilder":("dev_tooling",0.0), "stylus-sdk-rs":("dev_tooling",0.0),
    "remix-project":("dev_tooling",0.0), "trueblocks-core":("dev_tooling",0.0),
    "ethdo":("dev_tooling",0.0), "otterscan":("dev_tooling",0.0),
    "web3.py":("dev_tooling",0.0), "web3j":("dev_tooling",0.0),
    "commit-boost-client":("dev_tooling",0.0),
    "ethstaker-deposit-cli":("dev_tooling",0.0),
    "l2beat":("dev_tooling",0.0), "swiss-knife":("dev_tooling",0.0),
    "ape":("dev_tooling",0.0), "hardhat-deploy":("dev_tooling",0.0),
    "tevm-monorepo":("dev_tooling",0.0),
    "DefiLlama-Adapters":("dev_tooling",0.0), "blockscout":("dev_tooling",0.0),
    "scaffold-eth-2":("dev_tooling",0.0),
    "checkpointz":("config_infra",0.0), "DAppNode":("config_infra",0.0),
    "eth-docker":("config_infra",0.0), "ethereum-package":("config_infra",0.0),
    "simple-optimism-node":("config_infra",0.0),
    "chainlist":("data_repo",0.0), "chains":("data_repo",0.0),
}

def detect_category(row: pd.Series) -> tuple:
    name = row.get("repo_name", "")
    if name in _REPO_CATEGORY_OVERRIDES:
        return _REPO_CATEGORY_OVERRIDES[name]
    desc   = str(row.get("description","") or "").lower()
    topics = str(row.get("topics","")      or "").lower()
    lang   = str(row.get("language","")    or "")
    text   = desc + " " + topics
    for cat_name, bonus, keywords, langs in _CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            if not langs or lang in langs:
                return cat_name, bonus
    return "general", 0.0


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2c — NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════

_MAX_LOG_DEP     = np.log1p(1373)
_MAX_LOG_CODE    = np.log1p(58_165_882)
_MAX_LOG_CONTRIB = np.log1p(100)

def compute_normalization_constants(df: pd.DataFrame):
    global _MAX_LOG_CONTRIB, _MAX_LOG_DEP
    max_contrib      = df["contributor_count"].replace(0, np.nan).max()
    _MAX_LOG_CONTRIB = np.log1p(max_contrib) if max_contrib > 0 else np.log1p(1)
    # Use age-adjusted dep count for normalisation ceiling
    max_dep          = df["age_adj_dep_count"].max()
    _MAX_LOG_DEP     = np.log1p(max(max_dep, 400))
    log.info(f"  Norm: age_adj_dep_max={max_dep:.1f} (log={_MAX_LOG_DEP:.4f})  "
             f"contrib_max={max_contrib:.0f}")


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2d — CATEGORY CAPS (v42: further flattened)
# ══════════════════════════════════════════════════════════════════════════════

_CATEGORY_CAPS = {
    # v42: max spread = 0.78 - 0.50 = 28 pts. Soft cap lets strong repos exceed these.
    "data_repo":          0.50,
    "config_infra":       0.62,
    "dev_tooling":        0.74,
    "smart_contract":     0.75,
    "spec_standard":      0.75,
    "general":            0.75,
    "crypto_primitive":   0.78,
    "compiler_language":  0.78,
    "consensus_client":   0.78,
    "execution_client":   0.78,
    "zk_system":          0.78,
}

DEFAULT_WEIGHTS = {
    "dep_weight":          0.35,
    "code_weight":         0.22,
    "contrib_weight":      0.06,
    "ai_scratch_coef":     0.06,
    "ai_wrapper_coef":    -0.10,   # v43: was -0.08 — stronger wrapper penalty
    "ai_protocol_coef":    0.04,
    "ai_novel_coef":       0.03,
    "ai_found_coef":       0.08,
    "ai_template_coef":   -0.04,
    "direct_ratio_weight": 0.0,
    "dep_quality_weight":  0.0,
    "ai_glue_coef":        0.0,
    "ai_utility_coef":     0.0,
    "star_weight":         0.05,
    "activity_weight":     0.04,
    "usage_weight":       -0.03,
    "producer_weight":     0.04,   # v43: was 0.10 — compressed; now spread via sqrt
    "consumer_weight":    -0.04,
    "pc_ratio_weight":     0.02,   # v43: was 0.08 — reduced; distribution now saner
}

tuned_weights = DEFAULT_WEIGHTS.copy()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2e — BASE SCORE FORMULA
# ══════════════════════════════════════════════════════════════════════════════

def compute_base_score(row: pd.Series, weights: dict = None, caps: dict = None) -> float:
    if weights is None: weights = tuned_weights
    if caps    is None: caps    = _CATEGORY_CAPS

    # Age-adjusted dep score — penalises newer dep-heavy repos more than old ones
    adj_deps  = float(row.get("age_adj_dep_count", row["unified_dep_count"]) or 0)
    dep_score = 1.0 - (np.log1p(adj_deps) / _MAX_LOG_DEP)

    code_score    = np.log1p(row.get("total_code_bytes", 0)) / _MAX_LOG_CODE
    contrib       = max(0, int(row.get("contributor_count", 0) or 0))
    contrib_score = np.log1p(contrib) / _MAX_LOG_CONTRIB

    cat_name  = str(row.get("category", "general"))
    cat_bonus = float(row.get("cat_bonus", 0.0))

    readme_sig   = int(row.get("readme_tech_signal", 0) or 0)
    readme_bonus = float(np.clip(readme_sig * 0.02, -0.06, 0.06))
    bench_bonus  = 0.02 if row.get("has_benchmarks") == True else 0.0

    ai_score = float(np.clip(
        weights["ai_scratch_coef"]  * float(row.get("ai_scratch",  0) or 0) +
        weights["ai_protocol_coef"] * float(row.get("ai_protocol", 0) or 0) +
        weights["ai_novel_coef"]    * float(row.get("ai_novel",    0) or 0) +
        weights["ai_found_coef"]    * float(row.get("ai_found",    0) or 0) +
        weights["ai_wrapper_coef"]  * float(row.get("ai_wrapper",  0) or 0) +
        weights["ai_template_coef"] * float(row.get("ai_template", 0) or 0),
        -0.15, 0.15
    ))

    raw = (
        weights["dep_weight"]     * dep_score       +
        weights["code_weight"]    * code_score       +
        weights["contrib_weight"] * contrib_score    +
        cat_bonus + readme_bonus + bench_bonus + ai_score +
        weights.get("star_weight",      0.05) * float(row.get("star_score",     0) or 0) +
        weights.get("activity_weight",  0.04) * float(row.get("activity_score", 0) or 0) +
        weights.get("usage_weight",    -0.03) * float(row.get("usage_signal",   0) or 0) +
        weights.get("producer_weight",  0.04) * float(row.get("producer_score", 0) or 0) +
        weights.get("consumer_weight", -0.04) * float(row.get("consumer_score", 0) or 0) +
        weights.get("pc_ratio_weight",  0.02) * float(row.get("pc_ratio",       0) or 0)
    )

    base = 0.35 + (raw / 0.75) * 0.55
    cap  = caps.get(cat_name, 0.85)

    # v43: explicit config_infra penalty BEFORE soft cap
    # Age-adjusted dep penalty rewards old/stable/low-dep repos which
    # accidentally inflated config repos (DAppNode, ethereum-package).
    # A flat category-level correction prevents this without touching features.
    if cat_name == "config_infra":
        base -= 0.08

    # Soft cap: scores above cap taper at 40% rate rather than hard-clipping
    SOFT_DECAY = 0.40
    score = (cap + (base - cap) * SOFT_DECAY) if base > cap else base
    return round(float(max(score, 0.32)), 3)


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 3 — WEIGHT + CAP TUNING ON PUBLIC JURY DATA
# ══════════════════════════════════════════════════════════════════════════════

def tune_weights(df: pd.DataFrame):
    if not PUBLIC_JURY_FILE.exists():
        log.info("  originalityPublic.csv not found — using DEFAULT weights.")
        return None, None

    try:
        pub = pd.read_csv(PUBLIC_JURY_FILE)
        def _to_url(s):
            s = str(s).strip().rstrip("/")
            return s if s.startswith("https://") else f"https://github.com/{s}"
        pub["repo_url"] = pub["repo"].apply(_to_url)
        score_col = "average_originality" if "average_originality" in pub.columns else "originality"
        pub = pub.rename(columns={score_col: "manual_score"})
        pub = pub.merge(df[["repo_url"]], on="repo_url", how="inner")
    except Exception as e:
        log.warning(f"  Jury file failed: {e}")
        return None, None

    if len(pub) == 0:
        log.warning("  No jury repos matched — using DEFAULT weights.")
        return None, None

    log.info(f"  Tuning on {len(pub)} jury anchors from originalityPublic.csv")
    all_rows    = [df[df["repo_url"] == m["repo_url"]].iloc[0] for _, m in pub.iterrows()]
    all_targets = np.array(pub["manual_score"].values)

    FIXED = {
        "code_weight":         0.22,
        "contrib_weight":      0.06,
        "direct_ratio_weight": 0.0,
        "dep_quality_weight":  0.0,
        "ai_scratch_coef":     0.06,
        "ai_protocol_coef":    0.04,
        "ai_novel_coef":       0.03,
        "ai_found_coef":       0.08,
        "ai_template_coef":   -0.04,
        "ai_glue_coef":        0.0,
        "ai_utility_coef":     0.0,
        "star_weight":         0.05,
        "activity_weight":     0.04,
        "usage_weight":       -0.03,
        "producer_weight":     0.04,   # v43: reduced
        "consumer_weight":    -0.04,
        "pc_ratio_weight":     0.02,   # v43: reduced
    }

    dep_weights     = np.arange(0.25, 0.52, 0.03)
    ai_wrapper_vals = np.arange(-0.12, -0.02, 0.02)
    cap_vals        = np.arange(0.72, 0.84, 0.02)
    usage_weights   = np.arange(-0.06, 0.02, 0.02)

    total_p1 = len(dep_weights)*len(ai_wrapper_vals)*len(cap_vals)*len(usage_weights)
    log.info(f"  Phase 1: {total_p1} combos")

    best_mae, best_weights, best_single_cap = float('inf'), None, 0.78

    for dep_w, a_w, cap_v, usage_w in itertools.product(
            dep_weights, ai_wrapper_vals, cap_vals, usage_weights):
        w = {"dep_weight": dep_w, "ai_wrapper_coef": a_w,
             "ai_glue_coef": 0.0, "usage_weight": usage_w, **FIXED}
        trial_caps = _CATEGORY_CAPS.copy()
        for cat in ["execution_client","consensus_client","compiler_language",
                    "zk_system","crypto_primitive","smart_contract",
                    "spec_standard","general"]:
            trial_caps[cat] = cap_v
        preds = [compute_base_score(r, w, trial_caps) for r in all_rows]
        mae   = np.mean(np.abs(np.array(preds) - all_targets))
        if mae < best_mae:
            best_mae, best_weights, best_single_cap = mae, w.copy(), cap_v
            log.info(f"    MAE={mae:.4f} dep={dep_w:.2f} ai_w={a_w:.2f} "
                     f"cap={cap_v:.2f} usage_w={usage_w:.2f}")

    log.info(f"  Phase 1 done — MAE={best_mae:.4f}")

    # Phase 2: fine-tune 4 category caps only
    PHASE2_CATS = ["zk_system", "dev_tooling", "config_infra", "crypto_primitive"]
    cat_cap_ranges_p2 = {
        "zk_system":        np.arange(0.72, 0.88, 0.02),
        "dev_tooling":      np.arange(0.65, 0.80, 0.02),
        "config_infra":     np.arange(0.40, 0.68, 0.04),
        "crypto_primitive": np.arange(0.72, 0.88, 0.02),
    }
    best_caps = _CATEGORY_CAPS.copy()
    for cat in ["execution_client","consensus_client","compiler_language",
                "zk_system","crypto_primitive","smart_contract","spec_standard","general"]:
        best_caps[cat] = best_single_cap

    best_mae_p2    = float('inf')
    no_improve_cnt = 0
    for cap_combo in itertools.product(*[cat_cap_ranges_p2[c] for c in PHASE2_CATS]):
        trial_caps = best_caps.copy()
        for cat, v in zip(PHASE2_CATS, cap_combo):
            trial_caps[cat] = float(v)
        preds = [compute_base_score(r, best_weights, trial_caps) for r in all_rows]
        mae   = np.mean(np.abs(np.array(preds) - all_targets))
        if mae < best_mae_p2 - 0.0005:
            best_mae_p2, best_caps, no_improve_cnt = mae, trial_caps.copy(), 0
            log.info(f"    Phase2 MAE={mae:.4f} | "
                     + " ".join(f"{c}={trial_caps[c]:.2f}" for c in PHASE2_CATS))
        else:
            no_improve_cnt += 1
            if no_improve_cnt >= 500:
                log.info(f"  Early stopping at {no_improve_cnt} non-improving combos.")
                break

    log.info(f"  Phase 2 done — MAE={best_mae_p2:.4f}")

    # Leave-2-out CV
    log.info("  Leave-2-out CV...")
    n_anch, cv_errors = len(all_rows), []
    for i in range(n_anch):
        for j in range(i+1, n_anch):
            train_idxs    = [k for k in range(n_anch) if k not in (i,j)]
            train_rows    = [all_rows[k]    for k in train_idxs]
            train_targets = all_targets[train_idxs]
            cv_best_mae, cv_best_w = float('inf'), best_weights
            for dep_w in np.arange(0.28, 0.52, 0.06):
                for a_w in np.arange(-0.12, -0.02, 0.04):
                    w_cv = {**best_weights, "dep_weight": dep_w, "ai_wrapper_coef": a_w}
                    p    = [compute_base_score(r, w_cv, best_caps) for r in train_rows]
                    m    = np.mean(np.abs(np.array(p) - train_targets))
                    if m < cv_best_mae:
                        cv_best_mae, cv_best_w = m, w_cv
            for hi in (i, j):
                cv_errors.append(abs(
                    compute_base_score(all_rows[hi], cv_best_w, best_caps)
                    - all_targets[hi]
                ))
    cv_mae  = float(np.mean(cv_errors))
    overfit = cv_mae - best_mae_p2
    log.info(f"  CV MAE={cv_mae:.4f}  training MAE={best_mae_p2:.4f}  "
             f"gap={overfit:+.4f} "
             f"{'⚠️ overfit' if overfit > 0.04 else '✅ OK'}")

    return best_weights, best_caps


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 68)
    log.info("DEEP FUNDING II — Model 1 v43")
    log.info("  Fix 1: producer sqrt compression (no more spike distribution)")
    log.info("  Fix 2: producer_weight 0.10→0.04, pc_ratio 0.08→0.02")
    log.info("  Fix 3: config_infra explicit -0.08 penalty")
    log.info("  Fix 4: ai_wrapper_coef -0.08→-0.10")
    log.info("=" * 68 + "\n")

    df = load_data()

    log.info("\nComputing producer/consumer signals...")
    df = compute_producer_consumer_signals(df)

    log.info("\nComputing normalization constants...")
    compute_normalization_constants(df)

    log.info("Detecting categories...")
    df["category"], df["cat_bonus"] = zip(*df.apply(detect_category, axis=1))

    best_w, best_caps = tune_weights(df)

    if best_w is not None:
        global tuned_weights
        tuned_weights = best_w
        log.info("\nUsing TUNED weights.")
    else:
        log.info("\nUsing DEFAULT weights.")

    if best_caps is not None:
        global _CATEGORY_CAPS
        _CATEGORY_CAPS = best_caps
        log.info("Using LEARNED caps.")
    else:
        log.info("Using DEFAULT caps.")

    log.info("\nComputing final scores...")
    df["base_score"] = df.apply(lambda r: compute_base_score(r), axis=1)
    log.info(f"  mean={df['base_score'].mean():.3f}  "
             f"median={df['base_score'].median():.3f}  "
             f"min={df['base_score'].min():.3f}  "
             f"max={df['base_score'].max():.3f}")

    # Write full diagnostic output
    out_rows = []
    for _, row in df.iterrows():
        out_rows.append({
            "repo":           row["repo_url"],
            "originality":    row["base_score"],
            "category":       row["category"],
            "dep_count":      int(row["unified_dep_count"]),
            "age_years":      round(float(row.get("repo_age_years", 0)), 1),
            "age_adj_deps":   round(float(row.get("age_adj_dep_count", 0)), 1),
            "producer_score": round(float(row.get("producer_score", 0)), 3),
            "consumer_score": round(float(row.get("consumer_score", 0)), 3),
            "pc_ratio":       round(float(row.get("pc_ratio", 0)), 3),
            "direct_ratio":   round(float(row.get("direct_ratio", 1.0)), 3),
            "usage_signal":   round(float(row.get("usage_signal", 0)), 3),
            "star_score":     round(float(row.get("star_score", 0)), 3),
            "code_kb":        int(row.get("total_code_bytes", 0)) // 1000,
            "language":       row.get("language", ""),
            "stars":          int(row.get("stars", 0)),
        })

    with open(FORMULA_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    log.info(f"\nDiagnostic scores → {FORMULA_OUT}")

    # Write submission: repo,originality only
    with open(SUBMISSION_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["repo", "originality"])
        for r in out_rows:
            writer.writerow([r["repo"], round(float(r["originality"]), 4)])
    log.info(f"Submission        → {SUBMISSION_OUT}")

    log.info("\nFINAL SCORES (high → low):")
    log.info(f"  {'Repo':<38} {'Cat':<18} {'Age':>5} {'AdjDep':>7} "
             f"{'Prod':>6} {'Score':>7}")
    log.info("  " + "-" * 90)
    for r in sorted(out_rows, key=lambda x: -x["originality"]):
        name = r["repo"].split("/")[-1]
        log.info(f"  {name:<38} {r['category']:<18} "
                 f"{r['age_years']:>5.1f} {r['age_adj_deps']:>7.1f} "
                 f"{r['producer_score']:>6.3f} {r['originality']:>7.3f}")

    cats = Counter(r["category"] for r in out_rows)
    log.info("\nCATEGORY DISTRIBUTION:")
    for cat, n in cats.most_common():
        log.info(f"  {cat:<22} {n:>3}")
    log.info("=" * 68)


if __name__ == "__main__":
    main()