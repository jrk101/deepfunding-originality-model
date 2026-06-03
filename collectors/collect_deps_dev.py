"""
Deep Funding Level II — deps.dev Dependency Collector (v2 - Fixed)
====================================================================
The /projects/ endpoint only works for repos that publish packages to registries.
Most of our 98 repos are applications/frameworks, not published packages.

This script uses a DIRECT MAPPING: repo → (ecosystem, package_name)
and queries deps.dev by package name directly.

For repos with no published package (C++, Shell, config-only repos),
we fall back to the GitHub data we already have.

Ecosystems supported by deps.dev:  NPM, CARGO, GO, PYPI, MAVEN, RUBYGEMS, NUGET

Run: python collect_deps_dev.py
Output: data/deps_dev_data.csv
"""

import csv
import json
import time
import logging
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL = "https://api.deps.dev/v3alpha"
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "deps_dev_data.csv"
SLEEP = 0.4   # seconds between requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "deps_dev.log", mode="w"),
    ],
)
logger = logging.getLogger(__name__)

# ── PACKAGE MAPPING ───────────────────────────────────────────────────────────
# Format: "github_repo_url" -> [("ECOSYSTEM", "package_name"), ...]
# None = no published package (C++, Shell, config repos, etc.)

PACKAGE_MAP = {
    # ── Go repos ──────────────────────────────────────────────────────────────
    "https://github.com/ethpandaops/checkpointz":           [("GO", "github.com/ethpandaops/checkpointz")],
    "https://github.com/ethereum/go-ethereum":              [("GO", "github.com/ethereum/go-ethereum")],
    "https://github.com/erigontech/erigon":                 [("GO", "github.com/erigontech/erigon")],
    "https://github.com/OffchainLabs/prysm":                [("GO", "github.com/prysmaticlabs/prysm/v5")],
    "https://github.com/holiman/goevmlab":                  [("GO", "github.com/holiman/goevmlab")],
    "https://github.com/wealdtech/ethdo":                   [("GO", "github.com/wealdtech/ethdo")],
    "https://github.com/aestus-relay/mev-boost-relay":      [("GO", "github.com/aestus-relay/mev-boost-relay")],
    "https://github.com/flashbots/mev-boost-relay":         [("GO", "github.com/flashbots/mev-boost-relay")],
    "https://github.com/flashbots/mev-boost":               [("GO", "github.com/flashbots/mev-boost")],
    "https://github.com/NethermindEth/juno":                [("GO", "github.com/NethermindEth/juno")],
    "https://github.com/Consensys/gnark-crypto":            [("GO", "github.com/consensys/gnark-crypto")],
    "https://github.com/supranational/blst":                [("GO", "github.com/supranational/blst")],

    # ── Rust/Cargo repos ──────────────────────────────────────────────────────
    "https://github.com/paradigmxyz/reth":                  [("CARGO", "reth")],
    "https://github.com/foundry-rs/foundry":                [("CARGO", "foundry-cli")],
    "https://github.com/alloy-rs/alloy":                    [("CARGO", "alloy")],
    "https://github.com/a16z/helios":                       [("CARGO", "helios-client")],
    "https://github.com/axiom-crypto/snark-verifier":       [("CARGO", "snark-verifier")],
    "https://github.com/risc0/risc0-ethereum":              [("CARGO", "risc0-ethereum")],
    "https://github.com/OffchainLabs/stylus-sdk-rs":        [("CARGO", "stylus-sdk")],
    "https://github.com/succinctlabs/rsp":                  [("CARGO", "rsp-client")],
    "https://github.com/0xMiden/miden-vm":                  [("CARGO", "miden-vm")],
    "https://github.com/Cyfrin/aderyn":                     [("CARGO", "aderyn")],
    "https://github.com/Commit-Boost/commit-boost-client":  [("CARGO", "commit-boost")],
    "https://github.com/lambdaclass/ethrex":                [("CARGO", "ethrex")],
    "https://github.com/succinctlabs/sp1":                  [("CARGO", "sp1-sdk")],
    "https://github.com/succinctlabs/op-succinct":          [("CARGO", "op-succinct-client")],
    "https://github.com/EspressoSystems/jellyfish":         [("CARGO", "jf-plonk")],
    "https://github.com/lambdaclass/lambdaworks":           [("CARGO", "lambdaworks-math")],
    "https://github.com/Plonky3/Plonky3":                   [("CARGO", "p3-field")],
    "https://github.com/flashbots/rbuilder":                [("CARGO", "rbuilder")],
    "https://github.com/arkworks-rs/algebra":               [("CARGO", "ark-ff")],
    "https://github.com/sigp/lighthouse":                   [("CARGO", "lighthouse")],
    "https://github.com/powdr-labs/powdr":                  [("CARGO", "powdr-ast")],
    "https://github.com/grandinetech/grandine":             [("CARGO", "grandine")],
    "https://github.com/edb-rs/edb":                        [("CARGO", "edb")],
    "https://github.com/argotorg/fe":                       [("CARGO", "fe-compiler")],
    "https://github.com/erigontech/silkworm":               None,   # C++ — no package
    "https://github.com/evmts/tevm-monorepo":               [("CARGO", "tevm"), ("NPM", "tevm")],

    # ── TypeScript / npm repos ────────────────────────────────────────────────
    "https://github.com/wevm/viem":                         [("NPM", "viem")],
    "https://github.com/chainsafe/lodestar":                [("NPM", "@chainsafe/lodestar")],
    "https://github.com/ethers-io/ethers.js":               [("NPM", "ethers")],
    "https://github.com/nomicfoundation/hardhat":           [("NPM", "hardhat")],
    "https://github.com/wighawag/hardhat-deploy":           [("NPM", "hardhat-deploy")],
    "https://github.com/ethereum/js-ethereum-cryptography":  [("NPM", "@ethereumjs/rlp")],
    "https://github.com/paulmillr/noble-curves":            [("NPM", "@noble/curves")],
    "https://github.com/ChainSafe/bls":                     [("NPM", "@chainsafe/bls")],
    "https://github.com/remix-project-org/remix-project":   [("NPM", "@remix-project/remix-lib")],
    "https://github.com/argotorg/sourcify":                 [("NPM", "@ethereum-sourcify/lib-sourcify")],
    "https://github.com/protofire/solhint":                 [("NPM", "solhint")],
    "https://github.com/shazow/whatsabi":                   [("NPM", "@shazow/whatsabi")],
    "https://github.com/ethdebug/format":                   [("NPM", "@ethdebug/format")],
    "https://github.com/scaffold-eth/scaffold-eth-2":       None,   # web app / template
    "https://github.com/safe-global/safe-smart-account":    [("NPM", "@safe-global/safe-contracts")],
    "https://github.com/openzeppelin/openzeppelin-contracts": [("NPM", "@openzeppelin/contracts")],
    "https://github.com/swiss-knife-xyz/swiss-knife":       None,   # web app only
    "https://github.com/DefiLlama/DefiLlama-Adapters":      None,   # adapters repo
    "https://github.com/otterscan/otterscan":               None,   # web app only
    "https://github.com/l2beat/l2beat":                     None,   # web app only
    "https://github.com/dl-solarity/solidity-lib":          [("NPM", "@solarity/solidity-lib")],
    "https://github.com/Vectorized/solady":                 [("NPM", "solady")],
    "https://github.com/ethereum/execution-apis":           None,   # spec/docs repo
    "https://github.com/eth-infinitism/account-abstraction": [("NPM", "@account-abstraction/contracts")],
    "https://github.com/taikoxyz/taiko-mono":               [("NPM", "@taiko-labs/taiko-sdk")],

    # ── Python / PyPI repos ───────────────────────────────────────────────────
    "https://github.com/vyperlang/vyper":                   [("PYPI", "vyper")],
    "https://github.com/vyperlang/titanoboa":               [("PYPI", "titanoboa")],
    "https://github.com/apeworx/ape":                       [("PYPI", "eth-ape")],
    "https://github.com/ethereum/web3.py":                  [("PYPI", "web3")],
    "https://github.com/ethereum/py_ecc":                   [("PYPI", "py_ecc")],
    "https://github.com/ethereum/consensus-specs":          [("PYPI", "eth2spec")],
    "https://github.com/ethstaker/ethstaker-deposit-cli":   [("PYPI", "staking-deposit")],
    "https://github.com/a16z/halmos":                       [("PYPI", "halmos")],

    # ── Java / Maven repos ────────────────────────────────────────────────────
    "https://github.com/hyperledger/besu":                  [("MAVEN", "org.hyperledger.besu:besu")],
    "https://github.com/consensys/teku":                    [("MAVEN", "tech.pegasys.teku:teku")],
    "https://github.com/hyperledger-web3j/web3j":           [("MAVEN", "org.web3j:core")],
    "https://github.com/LFDT-web3j/web3j":                  [("MAVEN", "org.web3j:core")],

    # ── No package / unsupported ecosystem ────────────────────────────────────
    "https://github.com/argotorg/act":                      None,   # Haskell
    "https://github.com/argotorg/hevm":                     None,   # Haskell
    "https://github.com/argotorg/solidity":                 None,   # C++ compiler
    "https://github.com/status-im/nimbus-eth2":             None,   # Nim
    "https://github.com/TrueBlocks/trueblocks-core":        None,   # Shell/C++
    "https://github.com/ethpandaops/ethereum-helm-charts":  None,   # Helm charts
    "https://github.com/blockscout/blockscout":             None,   # Elixir app
    "https://github.com/ethereum/eips":                     None,   # docs
    "https://github.com/ethereum-lists/chains":             None,   # data repo
    "https://github.com/dappnode/DAppNode":                 None,   # Shell infra
    "https://github.com/ethstaker/eth-docker":              None,   # Docker/Shell
    "https://github.com/smartcontracts/simple-optimism-node": None, # Shell/config
    "https://github.com/ethpandaops/ethereum-package":      None,   # Starlark
    "https://github.com/DefiLlama/chainlist":               None,   # data/web
    "https://github.com/libp2p/libp2p":                     None,   # meta-repo
    "https://github.com/ipsilon/evmone":                    None,   # C++
    "https://github.com/skalenetwork/libBLS":               None,   # C++
    "https://github.com/herumi/mcl":                        None,   # C++
    "https://github.com/nethereum/nethereum":               None,   # .NET
    "https://github.com/nethermindeth/nethermind":          None,   # .NET
    "https://github.com/intellij-solidity/intellij-solidity": None, # IntelliJ plugin
    "https://github.com/Certora/CertoraProver":             None,   # closed-source
    "https://github.com/lambdaclass/lambda_ethereum_consensus": None, # Elixir app
}


# ── API HELPERS ───────────────────────────────────────────────────────────────

def api_get(url: str):
    for attempt in range(3):
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 404:
                return None
            elif e.code == 429:
                time.sleep(10 * (attempt + 1))
            else:
                logger.warning(f"HTTP {e.code}: {url}")
                time.sleep(5)
        except (URLError, Exception) as e:
            logger.warning(f"Error {url}: {e}")
            time.sleep(5)
    return None


def get_latest_version(system: str, name: str):
    url = f"{BASE_URL}/systems/{quote(system, safe='')}/packages/{quote(name, safe='')}"
    data = api_get(url)
    if not data:
        return None
    versions = data.get("versions", [])
    for v in versions:
        if v.get("isDefault"):
            return v.get("versionKey", {}).get("version")
    if versions:
        return versions[-1].get("versionKey", {}).get("version")
    return None


def get_dep_counts(system: str, name: str, version: str) -> dict:
    url = (f"{BASE_URL}/systems/{quote(system, safe='')}"
           f"/packages/{quote(name, safe='')}"
           f"/versions/{quote(version, safe='')}:dependencies")
    data = api_get(url)
    if not data:
        return {"direct": 0, "transitive": 0, "found": False}
    edges = data.get("edges", [])
    nodes = data.get("nodes", [])
    direct = sum(1 for e in edges if e.get("fromNode", -1) == 0)
    transitive = max(0, len(nodes) - 1)
    return {"direct": direct, "transitive": transitive, "found": True}


# ── MAIN ──────────────────────────────────────────────────────────────────────

def process_repo(repo_url: str) -> dict:
    result = {
        "repo_url": repo_url,
        "depsdev_found": False,
        "depsdev_ecosystem": "",
        "depsdev_package": "",
        "depsdev_version": "",
        "depsdev_direct_deps": 0,
        "depsdev_transitive_deps": 0,
    }

    packages = PACKAGE_MAP.get(repo_url)

    if packages is None:
        logger.info(f"  → Skipped (no published package for this repo)")
        return result

    total_direct = 0
    total_transitive = 0
    ecosystems_found = []
    packages_found = []

    for system, name in packages:
        time.sleep(SLEEP)
        version = get_latest_version(system, name)
        if not version:
            logger.info(f"  → {system}/{name}: NOT found on deps.dev")
            continue

        logger.info(f"  → {system}/{name}@{version}")
        time.sleep(SLEEP)
        counts = get_dep_counts(system, name, version)

        if counts["found"]:
            total_direct += counts["direct"]
            total_transitive += counts["transitive"]
            ecosystems_found.append(system)
            packages_found.append(name)
            result["depsdev_found"] = True
            result["depsdev_version"] = version
            logger.info(f"     ✓ direct={counts['direct']}, transitive={counts['transitive']}")
        else:
            logger.info(f"     ✗ No dependency graph for {system}/{name}")

    result["depsdev_ecosystem"] = ",".join(ecosystems_found)
    result["depsdev_package"] = ",".join(packages_found)
    result["depsdev_direct_deps"] = total_direct
    result["depsdev_transitive_deps"] = total_transitive
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
    logger.info("DEEP FUNDING — deps.dev Collector v2 (direct package lookup)")
    logger.info("=" * 70)

    repo_urls = list(PACKAGE_MAP.keys())
    logger.info(f"Processing {len(repo_urls)} repos\n")

    results = []
    for i, url in enumerate(repo_urls, 1):
        name = url.split("/")[-1]
        logger.info(f"[{i}/{len(repo_urls)}] {name}")
        r = process_repo(url)
        results.append(r)
        if i % 10 == 0:
            save(results)
            logger.info(f"  --- Checkpoint saved ({i}/{len(repo_urls)}) ---\n")

    save(results)

    found = sum(1 for r in results if r["depsdev_found"])
    logger.info(f"\n{'='*70}")
    logger.info(f"DONE — {found}/{len(results)} repos had dep data on deps.dev")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"\n{'Repo':<35} {'Ecosystem':<12} {'Direct':>8} {'Transitive':>12}")
    logger.info("-" * 72)
    for r in results:
        n = r["repo_url"].split("/")[-1]
        eco = r["depsdev_ecosystem"][:11] if r["depsdev_ecosystem"] else "N/A"
        logger.info(f"{n:<35} {eco:<12} {r['depsdev_direct_deps']:>8} {r['depsdev_transitive_deps']:>12}")


if __name__ == "__main__":
    main()
