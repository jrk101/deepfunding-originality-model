import csv, re, time, json, os, logging, sys, io, base64
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ── Windows UTF-8 fix ────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_FILE  = DATA_DIR / "readme_data.csv"
REPOS_FILE   = DATA_DIR / "github_repo_data.csv"
README_CHARS = 800   # chars to keep after stripping markdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "readme_collect.log", mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── GITHUB TOKEN ──────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SLEEP = 0.3 if GITHUB_TOKEN else 1.5
if GITHUB_TOKEN:
    log.info("GitHub token found — authenticated (5000 req/hr)")
else:
    log.warning("GITHUB_TOKEN not set — unauthenticated (60 req/hr)")


# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_json(url: str) -> dict | None:
    headers = {"User-Agent": "DeepFunding-ReadmeCollector/1.0",
               "Accept":     "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    for attempt in range(3):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                wait = 60 * (attempt + 1)
                log.warning(f"  Rate limited ({e.code}). Sleeping {wait}s...")
                time.sleep(wait); continue
            log.warning(f"  HTTP {e.code} attempt {attempt+1}: {url}")
            time.sleep(3)
        except Exception as e:
            log.warning(f"  Error attempt {attempt+1}: {e}")
            time.sleep(3)
    return None


# ── MARKDOWN STRIPPER ─────────────────────────────────────────────────────────
def strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)          # code blocks
    text = re.sub(r"`[^`\n]+`", " ", text)               # inline code
    text = re.sub(r"<[^>]+>", " ", text)                 # HTML tags
    text = re.sub(r"!\[.*?\]\(.*?\)", " ", text)         # images
    text = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", text)# links -> text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)     # bold/italic
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}",   r"\1", text)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)  # hr
    text = re.sub(r"https?://\S+", "", text)             # bare URLs
    text = re.sub(r"\[!\[.*?\].*?\]", "", text)          # badge links
    text = re.sub(r"\|[^\n]+\|", " ", text)              # tables
    text = re.sub(r"^[-|: ]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ── README FETCHER ────────────────────────────────────────────────────────────
def get_readme(owner: str, repo: str) -> tuple:
    """Returns (found: bool, excerpt: str)."""
    data = fetch_json(f"https://api.github.com/repos/{owner}/{repo}/readme")
    if not data or "content" not in data:
        return False, ""

    try:
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"  Decode error: {e}")
        return False, ""

    clean = strip_markdown(raw)
    if len(clean) < 80:
        return False, ""

    return True, clean[:README_CHARS].strip()


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("DEEP FUNDING II — README Collector")
    log.info(f"  Output: {OUTPUT_FILE}")
    log.info(f"  Sleep: {SLEEP}s per request")
    log.info("=" * 65 + "\n")

    # Load repo list
    repos = []
    if REPOS_FILE.exists():
        with open(REPOS_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                repos.append(row["repo_url"])
        log.info(f"Loaded {len(repos)} repos from {REPOS_FILE.name}\n")
    else:
        # fallback to repos_to_predict.csv
        fallback = BASE_DIR / "1773526020991_repos_to_predict.csv"
        if not fallback.exists():
            fallback = BASE_DIR / "repos_to_predict.csv"
        with open(fallback, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                repos.append(row["repo"])
        log.info(f"Loaded {len(repos)} repos from fallback list\n")

    # Resume support
    already_done = set()
    results = []
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            results = list(csv.DictReader(f))
            already_done = {r["repo_url"] for r in results}
        log.info(f"Resuming: {len(already_done)} done, "
                 f"{len(repos)-len(already_done)} remaining\n")

    found_count = sum(1 for r in results if r.get("readme_found") == "True")

    for i, repo_url in enumerate(repos, 1):
        if repo_url in already_done:
            log.info(f"[{i:>2}/98] {repo_url.split('/')[-1]} -- skipped")
            continue

        parts = repo_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            log.warning(f"[{i:>2}/98] Bad URL: {repo_url}")
            results.append({"repo_url": repo_url, "readme_found": "False",
                             "readme_excerpt": ""})
            continue

        owner, repo = parts[0], parts[1]
        log.info(f"[{i:>2}/98] {owner}/{repo}")

        found, excerpt = get_readme(owner, repo)
        if found:
            found_count += 1
            preview = excerpt[:80].replace("\n", " ")
            log.info(f"  OK  ({len(excerpt)} chars): {preview}...")
        else:
            log.info(f"  No README")

        results.append({"repo_url": repo_url,
                         "readme_found": str(found),
                         "readme_excerpt": excerpt})

        # Checkpoint every 10
        if i % 10 == 0:
            _save(results)
            log.info(f"  [Checkpoint saved — {i}/98]\n")

        time.sleep(SLEEP)

    _save(results)

    log.info("=" * 65)
    log.info(f"DONE — found: {found_count}/{len(repos)}")
    log.info(f"Output: {OUTPUT_FILE}")
    log.info("=" * 65)


def _save(rows):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["repo_url", "readme_found", "readme_excerpt"])
        w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    main()
