"""==============================
Layer 1 v9 — DeepFunding Level 2 Edition
==========================================================================

INPUTS (all in same folder as this script):
  repos_to_predict.csv                         — 98 seed repo URLs to clone & scan
  seedReposWithNoTransitiveDependencies.json   — maps each seed repo → [dep URLs]
  github_repo_data.csv                         — language + metadata for seed repos

WHAT THIS DOES:
  1. Fetches language for every dependency via GitHub API (cached to
     collected/dep_languages.json so we only hit the API once)
  2. Clones each of the 98 seed repos (shallow, depth=1)
  3. For each clone, scans source files to measure how heavily each
     dependency is actually used
  4. Outputs collected/usage_scores.csv — one row per (repo, dependency)

SCORE FORMULA (unchanged from v9):
  raw_freq   = imports×1 + aliases×2 + calls×3
  file_score = file_weight × log1p(raw_freq)
  total      = manifest_bonus + Σ(file_scores)

HOW TO RUN:
  pip install pandas tqdm requests
  set GITHUB_TOKEN=ghp_xxxx      (or paste directly into GITHUB_TOKEN below)
  python layer1_usage.py
"""

import os, re, json, time, subprocess, shutil, platform
import pandas as pd
import numpy as np
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ─────────────────────────────────────────────────────────────
# CONFIG — edit these paths if needed
# ─────────────────────────────────────────────────────────────

GITHUB_TOKEN  = ""   # paste token here OR set env var GITHUB_TOKEN

PREDICT_FILE  = "repos_to_predict.csv"
DEPS_JSON     = "seedReposWithNoTransitiveDependencies.json"
REPO_DATA_CSV = "github_repo_data.csv"

CLONE_DIR     = Path("cloned_seeds")
RESULTS_FILE  = Path("collected") / "usage_scores.csv"
DEP_LANG_CACHE = Path("collected") / "dep_languages.json"

CLONE_TIMEOUT = 300
CLONE_RETRIES = 3

# File location weights
CORE_WEIGHT   = 2.0
NORMAL_WEIGHT = 1.0
TEST_WEIGHT   = 0.3

# Usage type weights (within a file)
IMPORT_W  = 1
ALIAS_W   = 2
CALL_W    = 3

# Manifest bonus
MANIFEST_BONUS     = 3.0
DEV_MANIFEST_BONUS = MANIFEST_BONUS * 0.3

# Cap files per repo to avoid multi-hour scans on huge repos
MAX_FILES_PER_REPO = 2000

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "target", "dist", "build",
    "out", ".build", "_build", "generated", "gen", "third_party",
    "fixture", "fixtures", "testdata", "test_data",
}
CORE_DIRS = {"src", "lib", "core", "internal", "pkg", "crates", "cmd", "source", "main", "app"}

LANG_EXTENSIONS = {
    "rust":       [".rs"],
    "go":         [".go"],
    "python":     [".py"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "solidity":   [".sol"],
    "java":       [".java"],
    "csharp":     [".cs"],
    "cpp":        [".cpp", ".cc", ".cxx", ".h", ".hpp"],
    "ruby":       [".rb"],
    "nim":        [".nim"],
    "kotlin":     [".kt"],
    "haskell":    [".hs"],
    "elixir":     [".ex", ".exs"],
}
EXT_TO_LANG    = {ext: lang for lang, exts in LANG_EXTENSIONS.items() for ext in exts}
ALL_EXTENSIONS = set(EXT_TO_LANG.keys())

# ─────────────────────────────────────────────────────────────
# GITHUB LANGUAGE → internal lang key
# ─────────────────────────────────────────────────────────────

GITHUB_LANG_MAP = {
    "rust":        "rust",
    "go":          "go",
    "python":      "python",
    "typescript":  "typescript",
    "javascript":  "javascript",
    "solidity":    "solidity",
    "java":        "java",
    "c#":          "csharp",
    "c++":         "cpp",
    "ruby":        "ruby",
    "nim":         "nim",
    "kotlin":      "kotlin",
    "haskell":     "haskell",
    "elixir":      "elixir",
}

def gh_lang_to_internal(gh_lang: str) -> str:
    if not gh_lang:
        return ""
    return GITHUB_LANG_MAP.get(gh_lang.lower().strip(), "")


# ─────────────────────────────────────────────────────────────
# KNOWN OVERRIDES — dep slug → search terms
# ─────────────────────────────────────────────────────────────

KNOWN_OVERRIDES = {
    # Ethereum / ZK
    "ethereum/go-ethereum":          ["github.com/ethereum/go-ethereum"],
    "google/go-ethereum":            ["github.com/ethereum/go-ethereum"],
    "paradigmxyz/reth":              ["reth", "reth_primitives", "reth_db", "reth_network", "reth_rpc"],
    "bluealloy/revm":                ["revm", "revm_primitives", "revm_interpreter"],
    "foundry-rs/foundry":            ["foundry", "forge", "cast", "anvil", "foundry_common"],
    "alloy-rs/alloy":                ["alloy", "alloy_primitives", "alloy_provider", "alloy_network"],
    "alloy-rs/core":                 ["alloy_core", "alloy_primitives", "alloy_sol_types", "alloy_rlp"],
    "alloy-rs/eips":                 ["alloy_eips"],
    "alloy-rs/rlp":                  ["alloy_rlp"],
    "alloy-rs/trie":                 ["alloy_trie"],
    "alloy-rs/op-alloy":             ["op_alloy"],
    "lambdaclass/ethrex":            ["ethrex"],
    "erigontech/erigon":             ["github.com/erigontech/erigon"],
    "supranational/blst":            ["blst"],
    "facebook/winterfell":           ["winterfell"],
    "0xpolygonmiden/miden-vm":       ["miden", "miden_core", "miden_crypto", "miden_air"],
    "0xmiden/miden-vm":              ["miden", "miden_core", "miden_crypto", "miden_air"],
    "0xpolygonmiden/crypto":         ["miden_crypto"],
    "0xpolygonmiden/miden-formatting": ["miden_formatting"],
    "arkworks-rs/algebra":           ["ark_ff", "ark_ec", "ark_poly", "ark_serialize"],
    "arkworks-rs/r1cs-std":          ["ark_r1cs_std"],
    "arkworks-rs/groth16":           ["ark_groth16"],
    "arkworks-rs/snark":             ["ark_snark", "ark_relations"],
    "axiom-crypto/snark-verifier":   ["snark_verifier"],
    "consensys/gnark":               ["github.com/consensys/gnark"],
    "consensys/gnark-crypto":        ["github.com/consensys/gnark-crypto"],
    # Rust ecosystem
    "tokio-rs/tokio":                ["tokio"],
    "tokio-rs/tracing":              ["tracing", "tracing_subscriber", "tracing_core"],
    "tokio-rs/axum":                 ["axum"],
    "tokio-rs/bytes":                ["bytes"],
    "tokio-rs/loom":                 ["loom"],
    "rust-lang/log":                 ["log"],
    "rust-lang/libc":                ["libc"],
    "rust-lang/hashbrown":           ["hashbrown"],
    "rust-lang/regex":               ["regex"],
    "rust-lang/cc-rs":               ["cc"],
    "rust-lang/glob":                ["glob"],
    "rust-lang/futures-rs":          ["futures"],
    "rust-lang/backtrace-rs":        ["backtrace"],
    "rust-lang/rustc-hash":          ["rustc_hash"],
    "rust-lang/ena":                 ["ena"],
    "rust-lang-nursery/lazy-static.rs": ["lazy_static"],
    "dtolnay/syn":                   ["syn"],
    "dtolnay/quote":                 ["quote"],
    "dtolnay/anyhow":                ["anyhow"],
    "dtolnay/thiserror":             ["thiserror"],
    "dtolnay/serde":                 ["serde"],
    "dtolnay/serde-yaml":            ["serde_yaml"],
    "dtolnay/serde-untagged":        ["serde_untagged"],
    "dtolnay/enumn":                 ["enumn"],
    "dtolnay/semver":                ["semver"],
    "dtolnay/paste":                 ["paste"],
    "dtolnay/proc-macro2":           ["proc_macro2"],
    "serde-rs/serde":                ["serde"],
    "serde-rs/json":                 ["serde_json"],
    "serde-rs/yaml":                 ["serde_yaml"],
    "rayon-rs/rayon":                ["rayon"],
    "rayon-rs/either":               ["either"],
    "crossbeam-rs/crossbeam":        ["crossbeam", "crossbeam_channel"],
    "clap-rs/clap":                  ["clap"],
    "rust-num/num":                  ["num", "num_bigint", "num_traits", "num_integer"],
    "rust-num/num-bigint":           ["num_bigint"],
    "rust-num/num-traits":           ["num_traits"],
    "rust-num/num-derive":           ["num_derive"],
    "burntsushi/walkdir":            ["walkdir"],
    "burntsushi/memchr":             ["memchr"],
    "burntsushi/byteorder":          ["byteorder"],
    "burntsushi/aho-corasick":       ["aho_corasick"],
    "burntsushi/rust-snappy":        ["snappy"],
    "bytecodealliance/wasmtime":     ["wasmtime"],
    "kokakiwi/rust-hex":             ["hex"],
    "djc/rustc-version-rs":          ["rustc_version"],
    "assert-rs/predicates-rs":       ["predicates"],
    "assert-rs/assert_cmd":          ["assert_cmd"],
    "stebalien/term":                ["term"],
    "stebalien/tempfile":            ["tempfile"],
    "alacritty/vte":                 ["vte"],
    "eyre-rs/eyre":                  ["eyre"],
    "servo/rust-fnv":                ["fnv"],
    "servo/rust-url":                ["url"],
    "servo/rust-smallvec":           ["smallvec"],
    "amanieu/parking_lot":           ["parking_lot"],
    "amanieu/atomic-rs":             ["atomic"],
    "chronotope/chrono":             ["chrono"],
    "bheisler/criterion.rs":         ["criterion"],
    "peternator7/strum":             ["strum"],
    "modprog/derive-where":          ["derive_where"],
    "sigp/discv5":                   ["discv5"],
    "sigp/ethereum_ssz":             ["ssz", "ethereum_ssz"],
    "sigp/ethereum_hashing":         ["ethereum_hashing"],
    "libp2p/rust-libp2p":            ["libp2p"],
    "paritytech/parity-common":      ["ethereum_types", "rlp", "keccak_hash"],
    "paritytech/jsonrpsee":          ["jsonrpsee"],
    "paritytech/unsigned-varint":    ["unsigned_varint"],
    "zkcrypto/bls12_381":            ["bls12_381"],
    "sergiobenitez/figment":         ["figment"],
    "marshallpierce/rust-base64":    ["base64"],
    "soc/dirs-rs":                   ["dirs"],
    "xdg-rs/dirs":                   ["dirs"],
    "detegr/rust-ctrlc":             ["ctrlc"],
    "dotenv-rs/dotenv":              ["dotenv"],
    "rust-random/rand":              ["rand"],
    "rust-random/getrandom":         ["getrandom"],
    "rust-random/rngs":              ["rand_core", "rand_chacha"],
    "rustwasm/wasm-bindgen":         ["wasm_bindgen"],
    "rustwasm/gloo":                 ["gloo"],
    "rustwasm/console_error_panic_hook": ["console_error_panic_hook"],
    "rreverser/serde-wasm-bindgen":  ["serde_wasm_bindgen"],
    "plotters-rs/plotters":          ["plotters"],
    "mitsuhiko/insta":               ["insta"],
    "mitsuhiko/similar":             ["similar"],
    "dbrgn/tracing-test":            ["tracing_test"],
    "rust-bitcoin/rust-bitcoin":     ["bitcoin"],
    "rust-fuzz/arbitrary":           ["arbitrary"],
    "rust-itertools/itertools":      ["itertools"],
    "rust-cli/env_logger":           ["env_logger"],
    "rust-analyzer/smol_str":        ["smol_str"],
    "rust-analyzer/rowan":           ["rowan"],
    "indexmap-rs/indexmap":          ["indexmap"],
    "indexmap-rs/ordermap":          ["ordermap"],
    "toml-rs/toml":                  ["toml"],
    "tower-rs/tower":                ["tower"],
    "tower-rs/tower-http":           ["tower_http"],
    "async-rs/async-std":            ["async_std"],
    "gankra/thin-vec":               ["thin_vec"],
    "camino-rs/camino":              ["camino"],
    "salsa-rs/salsa":                ["salsa"],
    "maciejhirsz/logos":             ["logos"],
    "oxalica/async-lsp":             ["async_lsp"],
    "jeltef/derive_more":            ["derive_more"],
    "mre/futures-batch":             ["futures_batch"],
    "smol-rs/async-compat":          ["async_compat"],
    "bitflags/bitflags":             ["bitflags"],
    "lalrpop/lalrpop":               ["lalrpop", "lalrpop_util"],
    "xacrimon/dashmap":              ["dashmap"],
    "mzabaluev/unwrap-infallible":   ["unwrap_infallible"],
    "la10736/rstest":                ["rstest"],
    "frondeus/test-case":            ["test_case"],
    "fe-lang/dir-test":              ["dir_test"],
    "proptest-rs/proptest":          ["proptest"],
    "crate-ci/escargot":             ["escargot"],
    "jerry73204/serde-semver":       ["serde_semver"],
    "sanpii/dot2.rs":                ["dot2"],
    "pyros2097/rust-embed":          ["rust_embed"],
    "qwandor/anes-rs":               ["anes"],
    "hawkw/matchers":                ["matchers"],
    "utkarshkukreti/diff.rs":        ["diff"],
    "qnnokabayashi/tracing-forest":  ["tracing_forest"],
    "ssheldon/rust-block":           ["block"],
    "luser/strip-ansi-escapes":      ["strip_ansi_escapes"],
    "eminence/terminal-size":        ["terminal_size"],
    "andrewhickman/fs-err":          ["fs_err"],
    "whizsid/wasmtimer-rs":          ["wasmtimer"],
    "zesterer/pollster":             ["pollster"],
    "manishearth/elsa":              ["elsa"],
    "gimli-rs/object":               ["object"],
    "gimli-rs/gimli":                ["gimli"],
    "gimli-rs/addr2line":            ["addr2line"],
    "oyvindln/adler2":               ["adler2"],
    "frommi/miniz_oxide":            ["miniz_oxide"],
    "danaugrs/overload":             ["overload"],
    "gfx-rs/metal-rs":               ["metal"],
    "rapidfuzz/strsim-rs":           ["strsim"],
    "vorner/signal-hook":            ["signal_hook"],
    "kkawakam/rustyline":            ["rustyline"],
    "blake3-team/blake3":            ["blake3"],
    "xudong-huang/generator-rs":     ["generator"],
    "micahscopes/act-locally":       ["act_locally"],
    # RustCrypto
    "rustcrypto/aeads":              ["aes_gcm", "chacha20poly1305", "aes_gcm_siv"],
    "rustcrypto/hashes":             ["sha2", "sha3", "blake2", "md5", "sha1", "ripemd"],
    "rustcrypto/signatures":         ["ecdsa", "ed25519", "rsa"],
    "rustcrypto/elliptic-curves":    ["k256", "p256", "p384", "bls12_381"],
    "rustcrypto/utils":              ["subtle", "zeroize", "crypto_bigint"],
    "rustcrypto/formats":            ["der", "pkcs8", "x509_cert", "pem_rfc7468"],
    "rustcrypto/sponges":            ["keccak"],
    "rustcrypto/kdfs":               ["hkdf", "pbkdf2", "scrypt", "argon2"],
    "rustcrypto/macs":               ["hmac", "cmac"],
    "rustcrypto/universal-hashes":   ["polyval", "ghash"],
    "rustcrypto/stream-ciphers":     ["chacha20", "salsa20"],
    "rustcrypto/block-ciphers":      ["aes", "des"],
    "rustcrypto/traits":             ["cipher", "digest", "signature", "crypto_common"],
    "rustcrypto/block-modes":        ["cbc", "ecb", "cfb_mode"],
    # Go ecosystem
    "golang/go":                     ["fmt", "os", "io", "net", "sync", "context"],
    "golang/protobuf":               ["github.com/golang/protobuf", "google.golang.org/protobuf"],
    "grpc/grpc-go":                  ["google.golang.org/grpc"],
    "uber-go/zap":                   ["go.uber.org/zap"],
    "uber-go/multierr":              ["go.uber.org/multierr"],
    "uber-go/atomic":                ["go.uber.org/atomic"],
    "stretchr/testify":              ["github.com/stretchr/testify"],
    "gorilla/mux":                   ["github.com/gorilla/mux"],
    "spf13/cobra":                   ["github.com/spf13/cobra"],
    "sirupsen/logrus":               ["github.com/sirupsen/logrus"],
    "prometheus/client_golang":      ["github.com/prometheus/client_golang"],
    "redis/go-redis":                ["github.com/redis/go-redis", "github.com/go-redis/redis"],
    "jmoiron/sqlx":                  ["github.com/jmoiron/sqlx"],
    "holiman/uint256":               ["github.com/holiman/uint256"],
    "btcsuite/btcd":                 ["github.com/btcsuite/btcd"],
    "pkg/errors":                    ["github.com/pkg/errors"],
    "goccy/go-json":                 ["github.com/goccy/go-json"],
    "flashbots/go-utils":            ["github.com/flashbots/go-utils"],
    "flashbots/go-boost-utils":      ["github.com/flashbots/go-boost-utils"],
    "attestantio/go-builder-client": ["github.com/attestantio/go-builder-client"],
    "attestantio/go-eth2-client":    ["github.com/attestantio/go-eth2-client"],
    "ferranbt/fastssz":              ["github.com/ferranbt/fastssz"],
    "prysmaticlabs/gohashtree":      ["github.com/prysmaticlabs/gohashtree"],
    # Python ecosystem
    "psf/requests":                  ["requests"],
    "pallets/flask":                 ["flask"],
    "pallets/click":                 ["click"],
    "django/django":                 ["django"],
    "numpy/numpy":                   ["numpy"],
    "pandas-dev/pandas":             ["pandas"],
    "web3py/web3.py":                ["web3"],
    "ethereum/eth-abi":              ["eth_abi"],
    "ethereum/eth-account":          ["eth_account"],
    "ethereum/eth-utils":            ["eth_utils"],
    "ethereum/eth-typing":           ["eth_typing"],
    "ethereum/hexbytes":             ["hexbytes"],
    "ethereum/py-trie":              ["trie"],
    "ethereum/py-geth":              ["geth"],
    "pydantic/pydantic":             ["pydantic"],
    "pydantic/pydantic-settings":    ["pydantic_settings"],
    "pytest-dev/pytest":             ["pytest"],
    "pytest-dev/pluggy":             ["pluggy"],
    "sqlalchemy/sqlalchemy":         ["sqlalchemy"],
    "tqdm/tqdm":                     ["tqdm"],
    "textualize/rich":               ["rich"],
    "yaml/pyyaml":                   ["yaml"],
    "pypa/packaging":                ["packaging"],
    "ipython/ipython":               ["IPython"],
    "ipython/traitlets":             ["traitlets"],
    "icrar/ijson":                   ["ijson"],
    "gorakhargosh/watchdog":         ["watchdog"],
    "xonsh/lazyasd":                 ["lazyasd"],
    "bobthebuidler/cchecksum":       ["cchecksum"],
    "dateutil/dateutil":             ["dateutil"],
    "uiri/toml":                     ["toml"],
    "urllib3/urllib3":               ["urllib3"],
    "gristlabs/asttokens":           ["asttokens"],
    "samypr100/backports.asyncio.runner": ["backports"],
    # JS/TS ecosystem
    "webpack/webpack":               ["webpack"],
    "microsoft/typescript":          ["typescript"],
    "expressjs/express":             ["express"],
    "sindresorhus/got":              ["got"],
    "uuidjs/uuid":                   ["uuid"],
    "nomicfoundation/hardhat":       ["hardhat"],
    "prettier/prettier":             ["prettier"],
    "wevm/viem":                     ["viem"],
    "wevm/wagmi":                    ["wagmi"],
    "ethers-io/ethers.js":           ["ethers"],
    "paulmillr/noble-curves":        ["noble-curves", "noble_curves"],
    "paulmillr/noble-hashes":        ["noble-hashes", "noble_hashes"],
    "ethereum/js-ethereum-cryptography": ["ethereum-cryptography"],
    "chainsafe/bls":                 ["bls"],
}


# ─────────────────────────────────────────────────────────────
# GITHUB API — fetch language for a dep slug
# ─────────────────────────────────────────────────────────────

def get_token() -> str:
    return GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN", "")


def fetch_dep_languages(dep_slugs: list) -> dict:
    """
    Fetches primary language for each dep slug via GitHub API.
    Results cached to DEP_LANG_CACHE so API is only hit once.
    Returns dict: slug -> internal_lang_string (e.g. "rust", "go", "python")
    """
    cache = {}
    DEP_LANG_CACHE.parent.mkdir(parents=True, exist_ok=True)

    if DEP_LANG_CACHE.exists():
        try:
            cache = json.loads(DEP_LANG_CACHE.read_text())
            print(f"  Loaded {len(cache)} cached dep languages from {DEP_LANG_CACHE}")
        except Exception:
            cache = {}

    token  = get_token()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    missing = [s for s in dep_slugs if s not in cache]
    if missing:
        print(f"  Fetching language for {len(missing)} deps via GitHub API...")
        for i, slug in enumerate(tqdm(missing, desc="GitHub API")):
            url = f"https://api.github.com/repos/{slug}"
            try:
                if HAS_REQUESTS:
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        cache[slug] = gh_lang_to_internal(data.get("language") or "")
                    elif resp.status_code == 404:
                        cache[slug] = ""
                    elif resp.status_code == 403:
                        # Rate limited — save what we have and stop
                        tqdm.write(f"  ⚠️  GitHub rate limit hit at {slug}. Saving cache.")
                        break
                    else:
                        cache[slug] = ""
                else:
                    # Fallback: use curl
                    cmd = ["curl", "-sf", "-H", f"Authorization: Bearer {token}",
                           "-H", "Accept: application/vnd.github+json", url]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if r.returncode == 0:
                        data = json.loads(r.stdout)
                        cache[slug] = gh_lang_to_internal(data.get("language") or "")
                    else:
                        cache[slug] = ""
            except Exception:
                cache[slug] = ""

            # Respect secondary rate limit — 1 req/sec safe, burst ok with token
            if (i + 1) % 30 == 0:
                time.sleep(1)

        DEP_LANG_CACHE.write_text(json.dumps(cache, indent=2))
        print(f"  Saved dep language cache → {DEP_LANG_CACHE}")

    return cache


# ─────────────────────────────────────────────────────────────
# SEARCH TERM BUILDER
# ─────────────────────────────────────────────────────────────

def build_search_terms(dep_slug: str, dep_language: str) -> list:
    # Normalise slug to lowercase for override lookup
    slug_lower = dep_slug.lower()
    if slug_lower in KNOWN_OVERRIDES:
        return [t for t in KNOWN_OVERRIDES[slug_lower] if t and len(t) >= 2]

    _, name = dep_slug.split("/", 1) if "/" in dep_slug else ("", dep_slug)
    lang    = (dep_language or "").lower()
    terms   = {name}

    cleaned = name
    for pat in ["-rs", "-rust", "-go", "-py", "-js", "-ts",
                "rust-", "go-", "py-", "node-", "js-", "-lib", "-core"]:
        cleaned = cleaned.replace(pat, "")
    if cleaned and cleaned != name and len(cleaned) >= 3:
        terms.add(cleaned)

    if lang == "rust":
        terms.add(name.replace("-", "_"))
        if cleaned:
            terms.add(cleaned.replace("-", "_"))

    return [t for t in terms if t and len(t) >= 3]


# ─────────────────────────────────────────────────────────────
# ALIAS EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_python_aliases(content: str, terms: list) -> list:
    aliases = []
    for term in terms:
        esc = re.escape(term)
        for m in re.finditer(rf'^import\s+{esc}\s+as\s+(\w+)', content, re.MULTILINE):
            aliases.append(m.group(1))
        for m in re.finditer(
            rf'^from\s+{esc}[\s.]\s*import\s+\w+\s+as\s+(\w+)', content, re.MULTILINE
        ):
            aliases.append(m.group(1))
    return aliases


def extract_js_aliases(content: str, terms: list) -> list:
    aliases = []
    for term in terms:
        esc = re.escape(term)
        for m in re.finditer(
            rf'''import\s+\*\s+as\s+(\w+)\s+from\s+['"][\w@/.-]*{esc}''', content
        ):
            aliases.append(m.group(1))
        for m in re.finditer(
            rf'''(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['"][\w@/.-]*{esc}''', content
        ):
            aliases.append(m.group(1))
        for m in re.finditer(
            rf'''import\s+(\w+)\s+from\s+['"][\w@/.-]*{esc}[\w@/.-]*['"]''', content
        ):
            name = m.group(1)
            if name not in ("type", "interface", "abstract"):
                aliases.append(name)
    return aliases


def count_alias_calls(content: str, aliases: list) -> int:
    total = 0
    for alias in aliases:
        if not alias or len(alias) < 2:
            continue
        esc = re.escape(alias)
        total += len(re.findall(rf'\b{esc}\.\w+', content))
    return total


# ─────────────────────────────────────────────────────────────
# CORE USAGE COUNTER
# ─────────────────────────────────────────────────────────────

def count_usage_in_file(content: str, terms: list, lang: str) -> dict:
    if not content or not terms:
        return {"import_count": 0, "alias_count": 0, "call_count": 0}

    total_imports = 0
    total_aliases = 0
    total_calls   = 0

    if lang == "python":
        aliases     = extract_python_aliases(content, terms)
        total_calls += count_alias_calls(content, aliases)
    elif lang in ("javascript", "typescript"):
        aliases     = extract_js_aliases(content, terms)
        total_calls += count_alias_calls(content, aliases)

    for term in terms:
        esc = re.escape(term)

        if lang == "rust":
            total_imports += len(re.findall(rf'\buse\s+{esc}\s*(::|\s*;|\s*\{{)', content))
            total_imports += len(re.findall(rf'\bextern\s+crate\s+{esc}\b', content))
            total_calls   += len(re.findall(rf'\b{esc}::\w+\s*[(\[]?', content))
            total_calls   += len(re.findall(rf'#\[(?:derive\([^)]*\b{esc}\b|{esc}\b)', content))

        elif lang == "go":
            total_imports += len(re.findall(rf'import\s+"[^"]*{esc}[^"]*"', content))
            total_imports += len(re.findall(rf'"[^"]*{esc}[^"]*"\s*\n', content))
            total_aliases += len(re.findall(rf'\w+\s+"[^"]*{esc}[^"]*"', content))
            pkg_name = term.split("/")[-1].replace("-", "")
            if pkg_name and len(pkg_name) >= 3:
                total_calls += len(re.findall(rf'\b{re.escape(pkg_name)}\.\w+\s*\(', content))

        elif lang == "python":
            total_imports += len(re.findall(rf'^import\s+{esc}\b', content, re.MULTILINE))
            total_imports += len(re.findall(rf'^from\s+{esc}[\s.]', content, re.MULTILINE))
            total_aliases += len(re.findall(rf'^import\s+{esc}\s+as\s+\w+', content, re.MULTILINE))
            total_calls   += len(re.findall(rf'\b{esc}\.\w+\s*[(\[]', content))

        elif lang in ("javascript", "typescript"):
            total_imports += len(re.findall(
                rf'''from\s+['"][\w@/.-]*{esc}[\w@/.-]*['"]''', content))
            total_imports += len(re.findall(
                rf'''require\s*\(\s*['"][\w@/.-]*{esc}[\w@/.-]*['"]\s*\)''', content))
            total_aliases += len(re.findall(
                rf'''import\s+\*\s+as\s+\w+\s+from\s+['"][\w@/.-]*{esc}''', content))
            pkg_name = term.split("/")[-1].replace("-", "").replace("@", "")
            if pkg_name and len(pkg_name) >= 3:
                total_calls += len(re.findall(
                    rf'\b{re.escape(pkg_name)}\.\w+\s*[(\[]', content))

        elif lang == "java":
            total_imports += len(re.findall(rf'^import\s+[\w.]*{esc}\b', content, re.MULTILINE))
            total_calls   += len(re.findall(rf'\b{esc}[\.\(]', content))

        elif lang == "csharp":
            total_imports += len(re.findall(rf'^using\s+[\w.]*{esc}\b', content, re.MULTILINE))
            total_calls   += len(re.findall(rf'\b{esc}\.\w+\s*[(\[]', content))

        elif lang == "ruby":
            total_imports += len(re.findall(rf'''require\s+['"][\w/-]*{esc}''', content))
            total_calls   += len(re.findall(rf'\b{esc}[\.:]\w+', content))

        elif lang == "nim":
            total_imports += len(re.findall(rf'^import\s+[\w/]*{esc}\b', content, re.MULTILINE))
            total_calls   += len(re.findall(rf'\b{esc}\.\w+', content))

        else:
            total_imports += len(re.findall(rf'''["\'`][\w/.-]*{esc}[\w/.-]*["\'`]''', content))
            total_calls   += len(re.findall(rf'\b{esc}[\.:]\w+', content))

    return {
        "import_count": total_imports,
        "alias_count":  total_aliases,
        "call_count":   total_calls,
    }


# ─────────────────────────────────────────────────────────────
# MANIFEST PARSERS
# ─────────────────────────────────────────────────────────────

def parse_cargo_toml(repo_path: Path) -> tuple:
    prod_deps, dev_deps = set(), set()
    for cargo_toml in repo_path.rglob("Cargo.toml"):
        parts_lower = [p.lower() for p in cargo_toml.parts]
        if any(s in parts_lower for s in ["vendor", ".cargo", "target"]):
            continue
        try:
            content = cargo_toml.read_text(encoding="utf-8", errors="ignore")
            for dep_block in re.finditer(
                r'^\[(dev-|build-)?dependencies(?:\.[^\]]+)?\](.*?)(?=^\[|\Z)',
                content, re.MULTILINE | re.DOTALL
            ):
                is_dev = dep_block.group(1) == "dev-"
                target = dev_deps if is_dev else prod_deps
                for line in dep_block.group(2).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r'^([a-zA-Z0-9_\-]+)\s*[=\.]', line)
                    if m:
                        d = m.group(1).strip()
                        target.add(d)
                        target.add(d.replace("-", "_"))
        except Exception:
            continue
    return prod_deps, dev_deps


def parse_go_mod(repo_path: Path) -> set:
    deps = set()
    for go_mod in repo_path.rglob("go.mod"):
        parts_lower = [p.lower() for p in go_mod.parts]
        if any(s in parts_lower for s in ["vendor", "testdata"]):
            continue
        try:
            content = go_mod.read_text(encoding="utf-8", errors="ignore")
            for block in re.findall(r'require\s*\(([^)]+)\)', content, re.DOTALL):
                for line in block.splitlines():
                    line = line.strip()
                    if line and not line.startswith("//"):
                        parts = line.split()
                        if parts:
                            deps.add(parts[0])
            for m in re.finditer(r'^require\s+([\w./\-]+)\s+v', content, re.MULTILINE):
                deps.add(m.group(1))
        except Exception:
            continue
    return deps


def parse_godeps(repo_path: Path) -> set:
    deps = set()
    for godeps in repo_path.rglob("Godeps.json"):
        try:
            data = json.loads(godeps.read_text(encoding="utf-8", errors="ignore"))
            for dep in data.get("Deps", []):
                ip = dep.get("ImportPath", "")
                if ip:
                    deps.add(ip)
                    parts = ip.split("/")
                    if len(parts) >= 3:
                        deps.add("/".join(parts[:3]))
        except Exception:
            continue
    return deps


def parse_npm_manifest_split(repo_path: Path) -> tuple:
    prod_deps, dev_deps = set(), set()
    for pkg_json in repo_path.rglob("package.json"):
        parts_lower = [p.lower() for p in pkg_json.parts]
        if any(s in parts_lower for s in ["node_modules", ".cache"]):
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            for key in ("dependencies", "peerDependencies", "optionalDependencies"):
                prod_deps.update(data.get(key, {}).keys())
            dev_deps.update(data.get("devDependencies", {}).keys())
        except Exception:
            continue
    return prod_deps, dev_deps


def parse_python_manifest(repo_path: Path) -> set:
    deps = set()
    for req in repo_path.rglob("requirements*.txt"):
        try:
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    name = re.split(r'[>=<!;\[]', line)[0].strip()
                    if name:
                        deps.add(name.lower().replace("-", "_"))
        except Exception:
            continue
    for pyproject in repo_path.rglob("pyproject.toml"):
        try:
            content = pyproject.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(
                r'^dependencies\s*=\s*\[([^\]]+)\]', content, re.MULTILINE | re.DOTALL
            ):
                for dep in re.findall(r'["\']([^"\'>=<!;\[]+)', m.group(1)):
                    deps.add(dep.strip().lower().replace("-", "_"))
        except Exception:
            continue
    return deps


def parse_csproj(repo_path: Path) -> set:
    deps = set()
    for csproj in list(repo_path.rglob("*.csproj")) + list(repo_path.rglob("*.fsproj")):
        try:
            content = csproj.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'<PackageReference\s+Include="([^"]+)"', content, re.IGNORECASE):
                pkg = m.group(1).strip()
                deps.add(pkg)
                deps.add(pkg.lower())
                deps.add(pkg.lower().replace(".", "_").replace("-", "_"))
        except Exception:
            continue
    return deps


def get_seed_manifest_deps(repo_path: Path) -> tuple:
    cargo_prod, cargo_dev = parse_cargo_toml(repo_path)
    npm_prod, npm_dev     = parse_npm_manifest_split(repo_path)

    prod_deps = set()
    dev_deps  = set()

    prod_deps.update(cargo_prod)
    prod_deps.update(npm_prod)
    prod_deps.update(parse_go_mod(repo_path))
    prod_deps.update(parse_godeps(repo_path))
    prod_deps.update(parse_python_manifest(repo_path))
    prod_deps.update(parse_csproj(repo_path))

    dev_deps.update(cargo_dev)
    dev_deps.update(npm_dev)

    return prod_deps, dev_deps


def manifest_match_check(dep_slug: str, search_terms: list, manifest_dep_names: set) -> bool:
    dep_lower = dep_slug.lower()
    for m in manifest_dep_names:
        m_lower = m.lower()
        if dep_lower in m_lower or m_lower.endswith(dep_lower):
            return True
    terms_norm    = {t.lower().replace("-", "_").replace(".", "_") for t in search_terms}
    manifest_norm = {m.lower().replace("-", "_").replace(".", "_") for m in manifest_dep_names}
    return bool(terms_norm & manifest_norm)


# ─────────────────────────────────────────────────────────────
# FILE UTILITIES
# ─────────────────────────────────────────────────────────────

def get_file_weight(file_path: Path, repo_root: Path) -> float:
    try:
        rel   = file_path.relative_to(repo_root)
        parts = [p.lower() for p in rel.parts[:-1]]
    except ValueError:
        return NORMAL_WEIGHT
    for part in parts:
        if any(t in part for t in ["test", "spec", "mock", "bench", "example"]):
            return TEST_WEIGHT
        if part in CORE_DIRS:
            return CORE_WEIGHT
    return NORMAL_WEIGHT


# ─────────────────────────────────────────────────────────────
# REPO SCANNER
# ─────────────────────────────────────────────────────────────

def scan_repo(repo_path: Path, deps_info: list) -> tuple:
    prod_deps, dev_deps = get_seed_manifest_deps(repo_path)

    results = {
        d["slug"]: {
            "weighted_score": 0.0,
            "manifest_match": False,
            "source_match":   False,
            "total_imports":  0,
            "total_calls":    0,
            "is_dev_only":    False,
            "test_calls":     0,
            "prod_calls":     0,
        }
        for d in deps_info
    }

    for dep_info in deps_info:
        slug  = dep_info["slug"]
        terms = dep_info["search_terms"]

        in_prod = manifest_match_check(slug, terms, prod_deps)
        in_dev  = manifest_match_check(slug, terms, dev_deps)

        if in_prod:
            results[slug]["manifest_match"]  = True
            results[slug]["weighted_score"] += MANIFEST_BONUS
        elif in_dev:
            results[slug]["manifest_match"]  = True
            results[slug]["is_dev_only"]     = True
            results[slug]["weighted_score"] += DEV_MANIFEST_BONUS

    # Collect source files, prioritised by importance
    core_files, normal_files, test_files = [], [], []
    for fpath in repo_path.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in ALL_EXTENSIONS:
            continue
        parts_lower = [p.lower() for p in fpath.parts]
        if any(s in parts_lower for s in SKIP_DIRS):
            continue
        is_test = any(t in parts_lower for t in ["test", "spec", "mock", "bench", "example"])
        is_core = any(p in parts_lower for p in CORE_DIRS)
        if is_test:
            test_files.append(fpath)
        elif is_core:
            core_files.append(fpath)
        else:
            normal_files.append(fpath)

    all_files = (core_files + normal_files + test_files)[:MAX_FILES_PER_REPO]
    if len(core_files) + len(normal_files) + len(test_files) > MAX_FILES_PER_REPO:
        skipped = len(core_files) + len(normal_files) + len(test_files) - MAX_FILES_PER_REPO
        tqdm.write(f"  ⚡ Large repo: capped at {MAX_FILES_PER_REPO} files (skipped {skipped})")

    for fpath in all_files:
        lang         = EXT_TO_LANG.get(fpath.suffix.lower(), "unknown")
        fw           = get_file_weight(fpath, repo_path)
        parts_f_low  = [p.lower() for p in fpath.parts]
        file_is_test = any(t in parts_f_low for t in ["test", "spec", "mock", "bench", "example"])

        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not content.strip():
            continue

        content_lower = content.lower()

        for dep_info in deps_info:
            slug  = dep_info["slug"]
            terms = dep_info["search_terms"]

            if not any(t.lower() in content_lower for t in terms):
                if lang not in ("python", "javascript", "typescript"):
                    continue

            counts  = count_usage_in_file(content, terms, lang)
            imports = counts["import_count"]
            aliases = counts["alias_count"]
            calls   = counts["call_count"]

            if imports + aliases + calls > 0:
                results[slug]["source_match"]   = True
                results[slug]["total_imports"] += imports
                results[slug]["total_calls"]   += calls

                if file_is_test:
                    results[slug]["test_calls"] += calls
                else:
                    results[slug]["prod_calls"] += calls

                capped_calls = min(calls, 50)
                raw_freq     = imports * IMPORT_W + aliases * ALIAS_W + capped_calls * CALL_W
                file_score   = fw * np.log1p(raw_freq)

                if results[slug]["is_dev_only"]:
                    file_score *= 0.15

                results[slug]["weighted_score"] += file_score

    # Post-scan: test-ratio penalty
    TEST_RATIO_THRESHOLD = 0.80
    TEST_RATIO_PENALTY   = 0.5
    for dep_info in deps_info:
        slug = dep_info["slug"]
        tc   = results[slug]["test_calls"]
        pc   = results[slug]["prod_calls"]
        if (tc + pc) > 5 and tc / (tc + pc) > TEST_RATIO_THRESHOLD:
            results[slug]["weighted_score"] *= TEST_RATIO_PENALTY

    return results, len(all_files)


# ─────────────────────────────────────────────────────────────
# GIT CLONE
# ─────────────────────────────────────────────────────────────

def count_source_files(repo_path: Path) -> int:
    return sum(1 for f in repo_path.rglob("*")
               if f.is_file() and f.suffix.lower() in ALL_EXTENSIONS)


def _delete_path(p: Path):
    if not p.exists():
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(["rmdir", "/s", "/q", str(p)],
                           shell=True, capture_output=True, timeout=30)
        else:
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def clone_repo(repo_slug: str, target_path: Path) -> bool:
    if target_path.exists() and count_source_files(target_path) > 0:
        n = count_source_files(target_path)
        tqdm.write(f"  📁 Already on disk ({n} source files) — skipping clone.")
        return True

    token = get_token()
    url   = (f"https://{token}@github.com/{repo_slug}.git"
             if token else f"https://github.com/{repo_slug}.git")

    cmd = [
        "git", "-c", "core.longpaths=true", "-c", "core.protectNTFS=false",
        "clone", "--depth", "1", "--single-branch", "--quiet",
        url, str(target_path)
    ]

    for attempt in range(1, CLONE_RETRIES + 1):
        if target_path.exists() and count_source_files(target_path) == 0:
            _delete_path(target_path)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT)
            if r.returncode == 0:
                return True

            err = r.stderr.strip()
            is_partial = any(kw in err.lower() for kw in [
                "checkout failed", "clone succeeded",
                "unable to create file", "invalid path", "filename too long"
            ])
            if is_partial and target_path.exists():
                n = count_source_files(target_path)
                if n > 0:
                    tqdm.write(f"  ⚠️  Partial checkout. {n} source files — proceeding.")
                    return True

            tqdm.write(f"  ⚠️  Attempt {attempt}/{CLONE_RETRIES}: {err[:100]}")

        except subprocess.TimeoutExpired:
            tqdm.write(f"  ⚠️  Timeout attempt {attempt}/{CLONE_RETRIES}")
        except Exception as e:
            tqdm.write(f"  ⚠️  Error attempt {attempt}/{CLONE_RETRIES}: {e}")

        if attempt < CLONE_RETRIES:
            time.sleep(5 * attempt)

    tqdm.write(f"  ❌ All {CLONE_RETRIES} attempts failed: {repo_slug}")
    return False


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def url_to_slug(url: str) -> str:
    """Convert https://github.com/owner/repo  →  owner/repo"""
    return url.rstrip("/").split("github.com/")[-1]


def main():
    print("=" * 65)
    print(" Deep Funding Level 2 — Layer 1 v9")
    print("=" * 65)
    print(" Score = manifest_bonus + Σ(file_weight × log1p(imports×1 + aliases×2 + calls×3))")
    print("=" * 65)

    if platform.system() == "Windows":
        subprocess.run(["git", "config", "--global", "core.longpaths", "true"],
                       capture_output=True)
        subprocess.run(["git", "config", "--global", "core.protectNTFS", "false"],
                       capture_output=True)

    token = get_token()
    print(f"\n  GitHub token: {'set ✅' if token else 'not set ⚠️  (clone may rate-limit)'}")

    # ── Load inputs ──────────────────────────────────────────
    predict_df = pd.read_csv(PREDICT_FILE)
    seed_urls  = predict_df["repo"].str.strip().tolist()
    seed_slugs = [url_to_slug(u) for u in seed_urls]

    with open(DEPS_JSON) as f:
        deps_raw = json.load(f)
    # Normalise keys to slug form, values to slug lists
    deps_map = {}
    for key, dep_url_list in deps_raw.items():
        seed_slug_key = url_to_slug(key)
        deps_map[seed_slug_key.lower()] = [url_to_slug(u) for u in dep_url_list]

    # ── Language lookup ──────────────────────────────────────
    # Step 1: use github_repo_data.csv for any dep that's also a seed repo
    repo_data_df = pd.read_csv(REPO_DATA_CSV)
    csv_lang_map = {}
    for _, row in repo_data_df.iterrows():
        slug = url_to_slug(str(row["repo_url"]))
        csv_lang_map[slug.lower()] = gh_lang_to_internal(str(row.get("language") or ""))

    # Step 2: collect all unique dep slugs
    all_dep_slugs = set()
    for deps in deps_map.values():
        all_dep_slugs.update(deps)

    # Step 3: fetch from GitHub API for deps not in CSV
    deps_needing_api = [s for s in all_dep_slugs if s.lower() not in csv_lang_map]
    print(f"\n  Seed repos:         {len(seed_slugs)}")
    print(f"  Total unique deps:  {len(all_dep_slugs)}")
    print(f"  In CSV (no API needed): {len(all_dep_slugs) - len(deps_needing_api)}")
    print(f"  Need GitHub API:    {len(deps_needing_api)}")

    api_lang_map = fetch_dep_languages(deps_needing_api)

    # Merge: CSV takes priority, then API
    def get_dep_lang(slug: str) -> str:
        s = slug.lower()
        if s in csv_lang_map:
            return csv_lang_map[s]
        return api_lang_map.get(slug, "")

    # ── Resume logic ─────────────────────────────────────────
    empty_df = pd.DataFrame({
        "repo":           pd.Series(dtype="str"),
        "dependency":     pd.Series(dtype="str"),
        "weighted_score": pd.Series(dtype="float64"),
        "manifest_match": pd.Series(dtype="bool"),
        "source_match":   pd.Series(dtype="bool"),
        "total_imports":  pd.Series(dtype="int64"),
        "total_calls":    pd.Series(dtype="int64"),
        "file_count":     pd.Series(dtype="int64"),
    })

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if RESULTS_FILE.exists():
        existing   = pd.read_csv(RESULTS_FILE)
        failed     = set(existing[existing["file_count"] == -1]["repo"].unique())
        done_repos = set(existing["repo"].unique()) - failed
        if failed:
            existing = existing[existing["file_count"] != -1]
            print(f"\n  Resuming: {len(done_repos)} done, retrying {len(failed)} failed")
        else:
            print(f"\n  Resuming: {len(done_repos)} repos already done")
    else:
        existing   = empty_df.copy()
        done_repos = set()

    CLONE_DIR.mkdir(exist_ok=True)

    remaining = [s for s in seed_slugs if s not in done_repos]
    print(f"  Remaining:  {len(remaining)} repos to process")
    print(f"  Clone dir:  {CLONE_DIR.resolve()}")
    print(f"  Output:     {RESULTS_FILE.resolve()}\n")

    new_rows = []

    for i, repo_slug in enumerate(tqdm(remaining, desc="Seed repos")):
        tqdm.write(f"\n[{i+1}/{len(remaining)}] {repo_slug}")

        repo_deps = deps_map.get(repo_slug.lower(), [])
        if not repo_deps:
            tqdm.write(f"  ⚠️  No deps found in JSON for {repo_slug} — skipping.")
            continue

        deps_info = [
            {
                "slug":         dep,
                "search_terms": build_search_terms(dep, get_dep_lang(dep)),
                "language":     get_dep_lang(dep),
            }
            for dep in repo_deps
        ]

        tqdm.write(f"  Deps: {len(repo_deps)} | Cloning...")
        repo_path = CLONE_DIR / repo_slug.replace("/", "_")
        success   = clone_repo(repo_slug, repo_path)

        if not success:
            for dep in repo_deps:
                new_rows.append({
                    "repo": repo_slug, "dependency": dep,
                    "weighted_score": 0.0, "manifest_match": False,
                    "source_match": False, "total_imports": 0,
                    "total_calls": 0, "file_count": -1,
                })
        else:
            tqdm.write(f"  Scanning...")
            scan_results, file_count = scan_repo(repo_path, deps_info)

            n_mani    = sum(1 for v in scan_results.values() if v["manifest_match"])
            n_src     = sum(1 for v in scan_results.values() if v["source_match"])
            tot_calls = sum(v["total_calls"] for v in scan_results.values())
            tqdm.write(
                f"  Files: {file_count} | "
                f"Manifest: {n_mani}/{len(repo_deps)} | "
                f"Source: {n_src}/{len(repo_deps)} | "
                f"Total calls: {tot_calls}"
            )

            for dep in repo_deps:
                r = scan_results.get(dep, {
                    "weighted_score": 0.0, "manifest_match": False,
                    "source_match": False, "total_imports": 0, "total_calls": 0,
                })
                new_rows.append({
                    "repo":           repo_slug,
                    "dependency":     dep,
                    "weighted_score": r["weighted_score"],
                    "manifest_match": r["manifest_match"],
                    "source_match":   r["source_match"],
                    "total_imports":  r["total_imports"],
                    "total_calls":    r["total_calls"],
                    "file_count":     file_count,
                })

            tqdm.write(f"  ✅ Done — repo kept on disk.")

        if new_rows:
            batch    = pd.DataFrame(new_rows)
            frames   = [f for f in [existing, batch] if len(f) > 0]
            combined = pd.concat(frames, ignore_index=True) if frames else empty_df
            combined.to_csv(RESULTS_FILE, index=False)
            existing = combined
            new_rows = []

    # ── Final summary ─────────────────────────────────────────
    print("\n" + "=" * 65)
    print("✅ Layer 1 v9 complete!")
    final      = pd.read_csv(RESULTS_FILE)
    success_df = final[final["file_count"] >= 0]
    n_mani  = success_df["manifest_match"].sum()
    n_src   = success_df["source_match"].sum()
    n_any   = (success_df["weighted_score"] > 0).sum()
    n       = max(len(success_df), 1)
    print(f"\n  Repos done:    {success_df['repo'].nunique()}")
    print(f"  Manifest hits: {n_mani} ({n_mani/n*100:.1f}%)")
    print(f"  Source hits:   {n_src}  ({n_src/n*100:.1f}%)")
    print(f"  Any usage > 0: {n_any}  ({n_any/n*100:.1f}%)")

    if "total_calls" in success_df.columns:
        print(f"  Total calls detected: {success_df['total_calls'].sum():,}")

    print(f"\n  Top 15 by weighted_score:")
    top = (success_df.groupby("dependency")["weighted_score"]
           .sum().sort_values(ascending=False).head(15))
    for dep, score in top.items():
        print(f"    {dep:<50} {score:.1f}")

    print(f"\n  Output: {RESULTS_FILE}")


if __name__ == "__main__":
    main()