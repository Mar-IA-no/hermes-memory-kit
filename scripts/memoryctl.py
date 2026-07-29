#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# v3.0: NO hardcoded fallbacks. Cascade resolves from env; if none is set,
# BASE_DIR / DB_PATH / HERMES_HOME remain None and _require_config() hard-fails
# any command that needs them. This prevents silent cross-agent pollution.
#
# Canonical var from v3.0: HMK_AGENT_MEMORY_BASE. HMK_BASE_DIR and
# AGENT_MEMORY_BASE remain accepted for back-compat.


def _resolve_base_dir():
    for k in ("HMK_AGENT_MEMORY_BASE", "AGENT_MEMORY_BASE", "HMK_BASE_DIR"):
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return None


def _resolve_hermes_home():
    for k in ("HMK_HERMES_HOME", "HERMES_HOME"):
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return None


BASE_DIR = _resolve_base_dir()
_db_env = os.environ.get("HMK_DB_PATH")
DB_PATH = (
    Path(_db_env).expanduser() if _db_env
    else ((BASE_DIR / "library.db") if BASE_DIR else None)
)
HERMES_HOME = _resolve_hermes_home()
_env_file_env = os.environ.get("HMK_ENV_FILE")
HERMES_ENV_PATH = (
    Path(_env_file_env).expanduser() if _env_file_env
    else ((HERMES_HOME / ".env") if HERMES_HOME else None)
)
WORKSPACE_ROOT = Path(os.environ.get("HMK_WORKSPACE_ROOT", str(Path.cwd()))).expanduser()


def _require_config(purpose: str = "this operation"):
    """Hard-fail if HMK_AGENT_MEMORY_BASE / HMK_DB_PATH cannot be resolved.

    Called by any command that reads/writes the library DB. Prevents silent
    fallback to a shared default path that would clobber another agent.
    """
    if BASE_DIR is None or DB_PATH is None:
        sys.stderr.write(
            f"ERROR: memoryctl needs HMK_AGENT_MEMORY_BASE (canonical) or\n"
            f"       AGENT_MEMORY_BASE / HMK_BASE_DIR / HMK_DB_PATH in the\n"
            f"       environment for {purpose}. Load your agent's .env before\n"
            f"       running — the 'hmk' wrapper does this automatically.\n"
            f"       See hermes-memory-kit v3.0 docs/migration-v3.md.\n"
        )
        sys.exit(2)
LEGACY_ROOT = os.environ.get("HMK_LEGACY_ROOT", "").strip()

# corpus_policy module (v3.9.0) — selective embedding + secret scan
try:
    from corpus_policy import (
        scan_content_for_secrets,
        should_block_file,
        classify_source_kind,
    )
except ImportError:
    scan_content_for_secrets = None  # type: ignore
    should_block_file = None  # type: ignore
    classify_source_kind = None  # type: ignore
DEFAULT_EMBED_PROVIDER = "nvidia"
DEFAULT_EMBED_MODELS = {
    "nvidia": "nvidia/llama-3.2-nemoretriever-300m-embed-v1",
    "google": "gemini-embedding-001",
    "local": "sentence-transformers/all-MiniLM-L6-v2",
    "model2vec": "minishlab/potion-retrieval-32M",
}
DEFAULT_EMBED_OUTPUT_DIMS = {
    "google": 768,
    "model2vec": 512,
}
LOCAL_MODEL_CACHE = {}
MODEL2VEC_CACHE = {}
FLASHRANK_CACHE = {}
PROJECT_QUERY_TERMS = {
    "hermes",
    "openclaw",
    "autoresearchclaw",
    "telegram",
    "roadmap",
    "bitacora",
    "memoria",
    "memory",
    "bibliotecario",
    "wiki",
    "obsidian",
    "skill",
    "agent",
    "agents",
    "agente",
    "soul",
    "codex",
    "gateway",
    "embedding",
    "embeddings",
}
BIBLIOTECA_PREFIX = os.environ.get("HMK_LIBRARY_CORPUS_PREFIX", "").strip()
META_PATH_PREFIXES = [str(WORKSPACE_ROOT / "docs")]
if BASE_DIR is not None:
    META_PATH_PREFIXES.append(str(BASE_DIR))
if HERMES_HOME is not None:
    META_PATH_PREFIXES.append(str(HERMES_HOME))
if LEGACY_ROOT:
    META_PATH_PREFIXES.append(LEGACY_ROOT)
META_EXACT_PATHS = {
    str(WORKSPACE_ROOT / "AGENTS.md"),
    str(WORKSPACE_ROOT / "bitacora.md"),
}

DEFAULT_SHELVES = {
    "identity": "Identity, principles, and highest-priority constraints",
    "state": "Current operating state and live context",
    "plans": "Plans, roadmap, architecture, backlog, and decisions",
    "episodes": "Chronological log and episodic memory",
    "library": "Distilled notes and reusable knowledge",
    "evidence": "Source documents, reports, and raw traces",
    # Minecraft-scoped shelves (v3.5+) — kept disjoint from the general
    # shelves so retrieval can filter by --shelf without contaminating
    # CLI/Telegram conversation memory with in-game noise.
    "mc-episodic": "Minecraft world events: what happened, who did what, when",
    "mc-social": "Minecraft social facts about other players/bots (alliances, conflicts, preferences)",
    "mc-skills": "Minecraft skills, playbooks, recipes, and patterns learned in-game",
    "mc-places": "Minecraft notable locations: base, mine, farm, danger zones, marks",
}


def _parse_csv(value):
    """Parse a CSV string into a list of trimmed non-empty strings.
    Accepts None, empty string, list, or already-parsed list."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _filter_clauses_and_params(shelves=None, exclude_shelves=None,
                               tags=None, exclude_tags=None,
                               engram_types=None,
                               shelf_table_alias="s", chapter_table_alias="c"):
    """Build SQL WHERE clauses and params for shelf/tag filters.

    Args expected as list/CSV-string. Returns (clauses_list, params_list).
    Empty inputs → empty clauses/params (no filter applied).

    Tag filter uses JSON1 (json_each on tags_json) for exact-match against
    individual tag values — NOT substring LIKE on the JSON blob (which would
    falsely match e.g. 'mc' inside 'mcp')."""
    clauses = []
    params = []
    sh_in = _parse_csv(shelves)
    sh_out = _parse_csv(exclude_shelves)
    tg_in = _parse_csv(tags)
    tg_out = _parse_csv(exclude_tags)
    if sh_in:
        placeholders = ",".join("?" * len(sh_in))
        clauses.append(f"{shelf_table_alias}.name IN ({placeholders})")
        params.extend(sh_in)
    if sh_out:
        placeholders = ",".join("?" * len(sh_out))
        clauses.append(f"{shelf_table_alias}.name NOT IN ({placeholders})")
        params.extend(sh_out)
    if tg_in:
        placeholders = ",".join("?" * len(tg_in))
        clauses.append(
            f"EXISTS (SELECT 1 FROM json_each({chapter_table_alias}.tags_json) "
            f"WHERE value IN ({placeholders}))"
        )
        params.extend(tg_in)
    if tg_out:
        placeholders = ",".join("?" * len(tg_out))
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM json_each({chapter_table_alias}.tags_json) "
            f"WHERE value IN ({placeholders}))"
        )
        params.extend(tg_out)
    et_in = _parse_csv(engram_types)
    if et_in:
        placeholders = ",".join("?" * len(et_in))
        clauses.append(f"{chapter_table_alias}.engram_type IN ({placeholders})")
        params.extend(et_in)
    return clauses, params

BOOTSTRAP_DOCS_ENV = os.environ.get("HMK_BOOTSTRAP_DOCS_JSON", "").strip()


def now_ts():
    return int(time.time())


def ensure_dirs():
    for name in ["identity", "state", "plans", "episodes", "library", "evidence", "index"]:
        (BASE_DIR / name).mkdir(parents=True, exist_ok=True)


def load_bootstrap_docs():
    if not BOOTSTRAP_DOCS_ENV:
        return []
    path = Path(BOOTSTRAP_DOCS_ENV).expanduser()
    if not path.exists():
        raise SystemExit(f"bootstrap docs json not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    docs = []
    for item in data:
        docs.append(
            (
                item["path"],
                item["shelf"],
                item["title"],
                item.get("tags", []),
            )
        )
    return docs


def connect():
    _require_config("opening the library DB")
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def migrate_add_embedding_bin(con):
    """Additive migration: add embedding_bin BLOB column if missing."""
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_embeddings'"
    ).fetchone()
    if not row:
        return
    columns = con.execute("PRAGMA table_info(chapter_embeddings)").fetchall()
    column_names = {col["name"] for col in columns}
    if "embedding_bin" not in column_names:
        con.execute("ALTER TABLE chapter_embeddings ADD COLUMN embedding_bin BLOB")
        con.commit()


def migrate_add_embed_disabled(con):
    """v3.9.0: add embed_disabled + embed_disable_reason to chapters."""
    columns = {col["name"] for col in con.execute("PRAGMA table_info(chapters)").fetchall()}
    if "embed_disabled" not in columns:
        con.execute("ALTER TABLE chapters ADD COLUMN embed_disabled INTEGER NOT NULL DEFAULT 0")
    if "embed_disable_reason" not in columns:
        con.execute("ALTER TABLE chapters ADD COLUMN embed_disable_reason TEXT")


def migrate_embedding_table(con):
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_embeddings'"
    ).fetchone()
    if not row:
        return

    columns = con.execute("PRAGMA table_info(chapter_embeddings)").fetchall()
    pk_columns = [col["name"] for col in columns if col["pk"]]
    needs_migration = pk_columns == ["chapter_id"]
    temp_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_embeddings_new'"
    ).fetchone()
    if not needs_migration:
        if temp_exists:
            con.execute("DROP TABLE chapter_embeddings_new")
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chapter_embeddings_provider_model
            ON chapter_embeddings(provider, model)
            """
        )
        return

    if temp_exists:
        con.execute("DROP TABLE chapter_embeddings_new")

    con.executescript(
        """
        CREATE TABLE chapter_embeddings_new (
          chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          input_text_hash TEXT NOT NULL,
          dims INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (chapter_id, provider, model)
        );

        INSERT INTO chapter_embeddings_new(
          chapter_id, provider, model, input_text_hash, dims, embedding_json, created_at, updated_at
        )
        SELECT
          chapter_id, provider, model, input_text_hash, dims, embedding_json, created_at, updated_at
        FROM chapter_embeddings;

        DROP TABLE chapter_embeddings;
        ALTER TABLE chapter_embeddings_new RENAME TO chapter_embeddings;
        CREATE INDEX idx_chapter_embeddings_provider_model
        ON chapter_embeddings(provider, model);
        """
    )


def init_db():
    _require_config("initializing the library DB")
    con = connect()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS shelves (
          id INTEGER PRIMARY KEY,
          name TEXT UNIQUE NOT NULL,
          description TEXT
        );

        CREATE TABLE IF NOT EXISTS books (
          id INTEGER PRIMARY KEY,
          shelf_id INTEGER NOT NULL REFERENCES shelves(id),
          slug TEXT NOT NULL,
          title TEXT NOT NULL,
          source_path TEXT,
          source_kind TEXT NOT NULL DEFAULT 'file',
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(shelf_id, slug)
        );

        CREATE TABLE IF NOT EXISTS chapters (
          id INTEGER PRIMARY KEY,
          book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL DEFAULT 1,
          title TEXT,
          spr TEXT NOT NULL,
          raw TEXT NOT NULL,
          tokens INTEGER NOT NULL DEFAULT 0,
          importance REAL NOT NULL DEFAULT 0.5,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          last_access INTEGER,
          access_count INTEGER NOT NULL DEFAULT 0,
          tags_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(
          title, spr, raw, tags,
          content='',
          tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS chapter_links (
          id INTEGER PRIMARY KEY,
          src_chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          dst_chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          link_type TEXT NOT NULL,
          weight REAL NOT NULL DEFAULT 1.0,
          note TEXT,
          created_at INTEGER NOT NULL,
          UNIQUE(src_chapter_id, dst_chapter_id, link_type)
        );

        CREATE TABLE IF NOT EXISTS queries_log (
          id INTEGER PRIMARY KEY,
          query_text TEXT NOT NULL,
          budget_tokens INTEGER NOT NULL,
          result_count INTEGER NOT NULL,
          null_retrieval INTEGER NOT NULL,
          details_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chapter_embeddings (
          chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          input_text_hash TEXT NOT NULL,
          dims INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          embedding_bin BLOB,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (chapter_id, provider, model)
        );

        CREATE TABLE IF NOT EXISTS link_suggestions (
          id INTEGER PRIMARY KEY,
          src_chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          dst_chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          score REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'candidate',
          created_at INTEGER NOT NULL,
          reviewed_at INTEGER,
          reviewer_note TEXT,
          UNIQUE(src_chapter_id, dst_chapter_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chapter_embeddings_provider_model
        ON chapter_embeddings(provider, model);
        """
    )
    migrate_embedding_table(con)
    migrate_add_embedding_bin(con)
    migrate_add_embed_disabled(con)
    for name, description in DEFAULT_SHELVES.items():
        con.execute(
            "INSERT OR IGNORE INTO shelves(name, description) VALUES(?, ?)",
            (name, description),
        )
    con.commit()
    con.close()


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def token_estimate(text):
    return max(1, math.ceil(len(text.split()) * 1.35))


def normalize_text(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def simple_spr(text, max_lines=8):
    text = normalize_text(text)
    if not text:
        return "- empty"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = []
    for line in lines:
        if line.startswith("#"):
            bullets.append(f"- heading: {line.lstrip('#').strip()}")
        elif line.startswith("- ") or line.startswith("* "):
            bullets.append(f"- {line[2:].strip()}")
        else:
            bullets.append(f"- {line[:140]}")
        if len(bullets) >= max_lines:
            break
    if not bullets:
        bullets = [f"- {text[:140]}"]
    return "\n".join(bullets)


def shelf_id(con, shelf_name):
    row = con.execute("SELECT id FROM shelves WHERE name=?", (shelf_name,)).fetchone()
    if not row:
        raise SystemExit(f"shelf not found: {shelf_name}")
    return row["id"]


def upsert_book(con, shelf_name, title, source_path=None, source_kind="file"):
    sid = shelf_id(con, shelf_name)
    slug = slugify(title)
    row = con.execute(
        "SELECT id FROM books WHERE shelf_id=? AND slug=?",
        (sid, slug),
    ).fetchone()
    if row:
        con.execute(
            "UPDATE books SET title=?, source_path=?, source_kind=?, updated_at=? WHERE id=?",
            (title, source_path, source_kind, now_ts(), row["id"]),
        )
        return row["id"]
    cur = con.execute(
        """
        INSERT INTO books(shelf_id, slug, title, source_path, source_kind, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (sid, slug, title, source_path, source_kind, now_ts(), now_ts()),
    )
    return cur.lastrowid


def clear_book_chapters(con, book_id):
    rows = con.execute(
        "SELECT id, title, spr, raw, tags_json FROM chapters WHERE book_id=?",
        (book_id,),
    ).fetchall()
    for row in rows:
        delete_chapter_fts(con, row)
    con.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))


def insert_chapter_fts(con, chapter_id, title, spr, raw, tags_json):
    con.execute(
        "INSERT INTO chapters_fts(rowid, title, spr, raw, tags) VALUES (?, ?, ?, ?, ?)",
        (chapter_id, title or "", spr, raw, tags_json),
    )


def delete_chapter_fts(con, row):
    con.execute(
        """
        INSERT INTO chapters_fts(chapters_fts, rowid, title, spr, raw, tags)
        VALUES('delete', ?, ?, ?, ?, ?)
        """,
        (row["id"], row["title"] or "", row["spr"], row["raw"], row["tags_json"] or "[]"),
    )


def add_text(shelf_name, title, raw, tags=None, importance=0.5, source_path=None, source_kind="text", replace=True):
    init_db()
    raw = normalize_text(raw)
    tags = tags or []
    spr = simple_spr(raw)
    con = connect()
    book_id = upsert_book(con, shelf_name, title, source_path=source_path, source_kind=source_kind)
    if replace:
        clear_book_chapters(con, book_id)

    # v3.9.0 — determine embed_disabled from content scan + source kind
    embed_disabled = 0
    embed_disable_reason = None
    if source_kind in ("code", "config"):
        embed_disabled = 1
        embed_disable_reason = f"source_kind={source_kind}"
    if embed_disabled == 0 and scan_content_for_secrets:
        secret_reason = scan_content_for_secrets(raw)
        if secret_reason:
            embed_disabled = 1
            embed_disable_reason = secret_reason

    cur = con.execute(
        """
        INSERT INTO chapters(book_id, ordinal, title, spr, raw, tokens, importance, created_at, updated_at, tags_json, embed_disabled, embed_disable_reason)
        VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            title,
            spr,
            raw,
            token_estimate(raw),
            float(importance),
            now_ts(),
            now_ts(),
            json.dumps(tags),
            embed_disabled,
            embed_disable_reason,
        ),
    )
    chapter_id = cur.lastrowid
    insert_chapter_fts(con, chapter_id, title, spr, raw, json.dumps(tags))
    con.commit()
    con.close()
    return chapter_id


def add_file(path, shelf_name, title=None, tags=None, importance=0.5, replace=True):
    p = Path(path)

    # v3.9.0 — file-level blocking via corpus policy
    if should_block_file:
        blocked, reason = should_block_file(path)
        if blocked:
            raise SystemExit(f"ERROR: file blocked by corpus policy: {reason}")

    raw = p.read_text(encoding="utf-8", errors="replace")

    # v3.9.0 — classify by extension for selective embedding
    kind = "file"
    if classify_source_kind:
        kind = classify_source_kind(path)

    return add_text(
        shelf_name=shelf_name,
        title=title or p.stem,
        raw=raw,
        tags=tags or [],
        importance=importance,
        source_path=str(p),
        source_kind=kind,
        replace=replace,
    )


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def read_env_key(name):
    if name in os.environ:
        return os.environ[name]
    # v3.0: HERMES_ENV_PATH may be None if neither HMK_ENV_FILE nor
    # HMK_HERMES_HOME / HERMES_HOME are set. In that case, fall back to
    # env-only lookup (above) and return None.
    if HERMES_ENV_PATH is not None and HERMES_ENV_PATH.exists():
        for line in HERMES_ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return None


def default_embed_provider():
    return (read_env_key("HERMES_EMBED_PROVIDER") or DEFAULT_EMBED_PROVIDER).strip().lower()


def default_embed_model(provider=None):
    provider = (provider or default_embed_provider()).strip().lower()
    generic = read_env_key("HERMES_EMBED_MODEL")
    specific = read_env_key(f"HERMES_EMBED_{provider.upper()}_MODEL")
    return generic or specific or DEFAULT_EMBED_MODELS.get(provider, DEFAULT_EMBED_MODELS["nvidia"])


def default_embed_output_dimensionality(provider=None):
    provider = (provider or default_embed_provider()).strip().lower()
    generic = read_env_key("HERMES_EMBED_OUTPUT_DIMS")
    specific = read_env_key(f"HERMES_EMBED_{provider.upper()}_OUTPUT_DIMS")
    raw = generic or specific
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise SystemExit(f"invalid embedding output dimensionality: {raw}") from exc
    return DEFAULT_EMBED_OUTPUT_DIMS.get(provider)


def normalize_embed_provider(provider=None):
    return (provider or default_embed_provider()).strip().lower()


def normalize_embed_model(provider=None, model=None):
    provider = normalize_embed_provider(provider)
    return model or default_embed_model(provider)


def google_task_type(input_type):
    return "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def embed_input_text(row):
    raw = normalize_text(row["raw"] or "")
    spr = normalize_text(row["spr"] or "")
    title = row["title"] or row.get("book_title") or ""
    body = raw[:5000]
    return f"title: {title}\n\nspr:\n{spr}\n\nbody:\n{body}".strip()


def embed_texts_nvidia(texts, input_type="passage", model=None):
    import urllib.request

    model = normalize_embed_model("nvidia", model)
    api_key = read_env_key("NVIDIA_API_KEY")
    if not api_key:
        raise SystemExit("missing NVIDIA_API_KEY")
    payload = json.dumps(
        {
            "model": model,
            "input": texts,
            "input_type": input_type,
            "encoding_format": "float",
            "truncate": "NONE",
        }
    ).encode()
    req = urllib.request.Request(
        "https://integrate.api.nvidia.com/v1/embeddings",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode())
    data = body.get("data", [])
    if len(data) != len(texts):
        raise SystemExit(f"unexpected embedding response size: expected {len(texts)}, got {len(data)}")
    return [item["embedding"] for item in data]


def embed_texts_google(texts, input_type="passage", model=None, output_dimensionality=None):
    import urllib.request

    model = normalize_embed_model("google", model)
    api_key = read_env_key("GEMINI_API_KEY") or read_env_key("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("missing GEMINI_API_KEY or GOOGLE_API_KEY")

    model_resource = model if model.startswith("models/") else f"models/{model}"
    requests = []
    for text in texts:
        item = {
            "model": model_resource,
            "content": {"parts": [{"text": text}]},
            "taskType": google_task_type(input_type),
        }
        if output_dimensionality:
            item["outputDimensionality"] = int(output_dimensionality)
        requests.append(item)

    payload = json.dumps({"requests": requests}).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/{model_resource}:batchEmbedContents",
        data=payload,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode())
    data = body.get("embeddings", [])
    if len(data) != len(texts):
        raise SystemExit(f"unexpected google embedding response size: expected {len(texts)}, got {len(data)}")
    vectors = []
    for item in data:
        values = item.get("values")
        if values is None:
            raise SystemExit("google embedding response missing values")
        vectors.append(values)
    return vectors


def embed_texts_local(texts, input_type="passage", model=None):
    model = normalize_embed_model("local", model)
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        raise SystemExit(
            "local embedding backend unavailable: install a compatible sentence-transformers stack first"
        ) from exc

    cache_key = (model,)
    if cache_key not in LOCAL_MODEL_CACHE:
        LOCAL_MODEL_CACHE[cache_key] = SentenceTransformer(model)
    encoder = LOCAL_MODEL_CACHE[cache_key]

    prepared = texts
    model_lower = model.lower()
    if "e5" in model_lower or "bge" in model_lower:
        prefix = "query: " if input_type == "query" else "passage: "
        prepared = [prefix + text for text in texts]

    vectors = encoder.encode(prepared, normalize_embeddings=True, convert_to_numpy=True)
    return [list(map(float, row.tolist())) for row in vectors]


def embed_texts_model2vec(texts, input_type="passage", model=None):
    """Static embeddings via Model2Vec (CPU-only, no torch in hot path).
    Returns list of float vectors. Default model: minishlab/potion-retrieval-32M.
    No prefix scheme (potion models don't expect query/passage tokens)."""
    model = normalize_embed_model("model2vec", model)
    try:
        from model2vec import StaticModel
    except Exception as exc:
        raise SystemExit(
            "model2vec backend unavailable: pip install model2vec (CPU-only static embeddings)"
        ) from exc
    cache_key = (model,)
    if cache_key not in MODEL2VEC_CACHE:
        MODEL2VEC_CACHE[cache_key] = StaticModel.from_pretrained(model)
    encoder = MODEL2VEC_CACHE[cache_key]
    vectors = encoder.encode(texts, show_progress_bar=False)
    return [list(map(float, row.tolist())) for row in vectors]


def embed_texts(provider, texts, input_type="passage", model=None, output_dimensionality=None):
    provider = normalize_embed_provider(provider)
    model = normalize_embed_model(provider, model)
    if provider == "nvidia":
        return embed_texts_nvidia(texts, input_type=input_type, model=model)
    if provider == "google":
        return embed_texts_google(
            texts,
            input_type=input_type,
            model=model,
            output_dimensionality=output_dimensionality,
        )
    if provider == "local":
        return embed_texts_local(texts, input_type=input_type, model=model)
    if provider == "model2vec":
        return embed_texts_model2vec(texts, input_type=input_type, model=model)
    raise SystemExit(f"unsupported embedding provider: {provider}")


def quantize_binary(vector):
    """Quantize a float32 vector to bit-packed bytes by sign.
    bit i = 1 iff vector[i] >= 0. Returns ceil(dim/8) bytes."""
    dim = len(vector)
    out = bytearray((dim + 7) // 8)
    for i, v in enumerate(vector):
        if v >= 0.0:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def hamming_distance(a, b):
    """Hamming distance between two equal-length bytes objects (popcount of XOR).
    Pure Python; for ~50k chapter scans this is fine. If perf becomes an issue
    later, replace with numpy uint8 view + bit-count."""
    if len(a) != len(b):
        return max(len(a), len(b)) * 8
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def cosine_similarity_packed(query_vec, passage_vec):
    """Float32 cosine similarity (rescore stage after binary ANN)."""
    return cosine_similarity(query_vec, passage_vec)


def flashrank_rerank(query, passages, model_name=None, cache_dir=None, top_k=None):
    """Rerank passages with FlashRank cross-encoder. Each passage must be a dict
    with at least 'id' and 'text' keys. Returns list of dicts with added 'score'
    and ordered desc by score. If FlashRank is not installed or load fails,
    returns passages unchanged.
    """
    model_name = (model_name or os.environ.get("HERMES_RERANK_MODEL")
                  or "ms-marco-TinyBERT-L-2-v2")
    cache_dir = cache_dir or os.environ.get("HERMES_RERANK_CACHE_DIR") or "/tmp/flashrank-cache"
    try:
        from flashrank import Ranker, RerankRequest
    except Exception:
        return passages
    cache_key = (model_name, cache_dir)
    if cache_key not in FLASHRANK_CACHE:
        try:
            FLASHRANK_CACHE[cache_key] = Ranker(model_name=model_name, cache_dir=cache_dir)
        except Exception as exc:
            sys.stderr.write(f"flashrank load failed: {exc} — falling back to no-rerank\n")
            FLASHRANK_CACHE[cache_key] = None
    ranker = FLASHRANK_CACHE[cache_key]
    if ranker is None:
        return passages
    try:
        req = RerankRequest(query=query, passages=passages)
        ranked = ranker.rerank(req)
    except Exception as exc:
        sys.stderr.write(f"flashrank rerank failed: {exc} — falling back to no-rerank\n")
        return passages
    if top_k:
        return ranked[:top_k]
    return ranked


def rerank_provider_default():
    """env-controlled rerank provider. flashrank|none. Default: flashrank if
    installed, else none."""
    explicit = (os.environ.get("HERMES_RERANK_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if importlib.util.find_spec("flashrank") is not None:
        return "flashrank"
    return "none"


def embeddings_runtime_config(provider=None, model=None):
    provider = normalize_embed_provider(provider)
    model = normalize_embed_model(provider, model)
    return {
        "provider": provider,
        "model": model,
        "output_dimensionality": default_embed_output_dimensionality(provider),
    }


def embeddings_capabilities():
    return {
        "default_provider": default_embed_provider(),
        "default_model": default_embed_model(),
        "rerank_provider": rerank_provider_default(),
        "providers": {
            "nvidia": {
                "configured": bool(read_env_key("NVIDIA_API_KEY")),
                "default_model": default_embed_model("nvidia"),
            },
            "google": {
                "configured": bool(read_env_key("GEMINI_API_KEY") or read_env_key("GOOGLE_API_KEY")),
                "default_model": default_embed_model("google"),
                "default_output_dimensionality": default_embed_output_dimensionality("google"),
            },
            "local": {
                "configured": bool(importlib.util.find_spec("sentence_transformers")),
                "default_model": default_embed_model("local"),
            },
            "model2vec": {
                "configured": bool(importlib.util.find_spec("model2vec")),
                "default_model": default_embed_model("model2vec"),
                "default_output_dimensionality": default_embed_output_dimensionality("model2vec"),
            },
        },
    }


def upsert_embedding(con, chapter_id, provider, model, source_text, vector):
    now = now_ts()
    binary = quantize_binary(vector)
    con.execute(
        """
        INSERT INTO chapter_embeddings(chapter_id, provider, model, input_text_hash, dims, embedding_json, embedding_bin, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chapter_id, provider, model) DO UPDATE SET
          provider=excluded.provider,
          model=excluded.model,
          input_text_hash=excluded.input_text_hash,
          dims=excluded.dims,
          embedding_json=excluded.embedding_json,
          embedding_bin=excluded.embedding_bin,
          updated_at=excluded.updated_at
        """,
        (
            chapter_id,
            provider,
            model,
            text_hash(source_text),
            len(vector),
            json.dumps(vector),
            binary,
            now,
            now,
        ),
    )


def tokenize_query(query):
    tokens = [t for t in re.findall(r"[a-zA-Z0-9_/-]+", query.lower()) if len(t) > 1]
    return tokens[:8]


def fts_query_string(query):
    toks = tokenize_query(query)
    if not toks:
        return f"\"{query.strip()}\""
    return " OR ".join(f"\"{tok}\"" for tok in toks)


def text_overlap_score(query, row):
    row = dict(row)
    query_tokens = set(tokenize_query(query))
    if not query_tokens:
        return 0.0
    title_text = " ".join(
        [
            row.get("title") or "",
            row.get("book_title") or "",
            row.get("tags_json") or "",
        ]
    ).lower()
    haystack = " ".join(
        [
            title_text,
            row.get("shelf") or "",
            row.get("spr") or "",
            (row.get("raw") or "")[:600],
        ]
    ).lower()
    hit_count = sum(1 for tok in query_tokens if tok in haystack)
    title_hits = sum(1 for tok in query_tokens if tok in title_text)
    base = hit_count / max(1, len(query_tokens))
    title_boost = min(0.35, title_hits * 0.12)
    return min(1.0, base + title_boost)


def is_project_query(query):
    return bool(set(tokenize_query(query)) & PROJECT_QUERY_TERMS)


def source_domain_prior(query, row):
    if is_project_query(query):
        return 0.0

    row_data = dict(row)
    path = (row_data.get("source_path") or "").strip()
    shelf = (row_data.get("shelf") or "").strip()

    if path.startswith(BIBLIOTECA_PREFIX):
        return 0.14
    if path in META_EXACT_PATHS:
        return -0.16
    if any(path.startswith(prefix) for prefix in META_PATH_PREFIXES):
        return -0.16
    if shelf in {"identity", "state", "plans", "episodes"}:
        return -0.10
    return 0.0


def score_candidates(rows, query):
    if not rows:
        return []
    now = now_ts()
    scored = []
    for rank, row in enumerate(rows, start=1):
        recency_days = max(0.0, (now - (row["updated_at"] or row["created_at"])) / 86400.0)
        recency = math.exp(-recency_days / 30.0)
        importance = max(0.0, min(1.0, float(row["importance"])))
        retrieval_rank = 1.0 / rank
        lexical = text_overlap_score(query, row)
        domain_prior = source_domain_prior(query, row)
        score = 0.10 * recency + 0.20 * importance + 0.25 * retrieval_rank + 0.45 * lexical + domain_prior
        scored.append((score, dict(row)))
    return scored


def overlap_ratio(a, b):
    ta = set(tokenize_query(a))
    tb = set(tokenize_query(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def search(query, limit=12, shelves=None, exclude_shelves=None,
           tags=None, exclude_tags=None, engram_types=None):
    init_db()
    con = connect()
    q = fts_query_string(query)
    extra_clauses, extra_params = _filter_clauses_and_params(
        shelves=shelves, exclude_shelves=exclude_shelves,
        tags=tags, exclude_tags=exclude_tags,
        engram_types=engram_types,
    )
    extra_sql = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
    rows = con.execute(
        f"""
        SELECT
          c.id,
          c.book_id,
          c.title,
          c.spr,
          c.raw,
          c.tokens,
          c.importance,
          c.created_at,
          c.updated_at,
          c.last_access,
          c.access_count,
          c.tags_json,
          b.title AS book_title,
          b.source_path,
          s.name AS shelf,
          bm25(chapters_fts) AS bm25_score
        FROM chapters_fts
        JOIN chapters c ON c.id = chapters_fts.rowid
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE chapters_fts MATCH ?{extra_sql}
        LIMIT ?
        """,
        (q, *extra_params, limit * 3),
    ).fetchall()
    con.close()
    scored = score_candidates(rows, query)
    out = []
    for score, row in sorted(scored, key=lambda item: item[0], reverse=True):
        row["score"] = round(score, 4)
        row["tags"] = json.loads(row["tags_json"] or "[]")
        out.append(row)
    return out[:limit]


def linked_neighbors(chapter_id):
    init_db()
    con = connect()
    rows = con.execute(
        """
        SELECT l.link_type, l.weight, c.id, c.title, c.spr, b.title AS book_title, s.name AS shelf
        FROM chapter_links l
        JOIN chapters c ON c.id = l.dst_chapter_id
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE l.src_chapter_id=?
        ORDER BY l.weight DESC, c.updated_at DESC
        LIMIT 12
        """,
        (chapter_id,),
    ).fetchall()
    con.close()
    return [dict(row) for row in rows]


# --- maintenance lock (v3.9.0, flock) ---


def _lock_maintenance():
    """Acquire an exclusive flock on the maintenance lock file.

    Non-blocking: fails immediately with exit code 3 if another
    process holds the lock.  Used by batch commands (embed-backfill,
    bootstrap, migration) to prevent interleaved runs.

    Read-only commands (stats, search, pack, expand, query, etc.)
    and single-chapter mutations (add_text/add_file/update/delete)
    do NOT take this lock — WAL handles those.
    """
    if BASE_DIR is None:
        return  # no-op when config isn't set (tests)
    lock_path = BASE_DIR / ".maintenance.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise SystemExit(
            f"ERROR: cannot open maintenance lock at {lock_path}: {exc}"
        )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(
            "ERROR: another maintenance process holds the lock.\n"
            f"  Lock file: {lock_path}\n"
            "  If you are certain no process is running, remove the lock file manually."
        )
    return fd


def _unlock_maintenance(fd):
    """Release flock and close fd."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def embedding_candidates(provider=None, model=None, limit=0, only_missing=True):
    provider = normalize_embed_provider(provider)
    model = normalize_embed_model(provider, model)
    init_db()
    con = connect()
    sql = """
        SELECT
          c.id,
          c.title,
          c.spr,
          c.raw,
          c.updated_at,
          b.title AS book_title
        FROM chapters c
        JOIN books b ON b.id = c.book_id
    """
    params = []
    if only_missing:
        sql += """
        LEFT JOIN chapter_embeddings e
          ON e.chapter_id = c.id AND e.provider = ? AND e.model = ?
        WHERE c.embed_disabled = 0 AND e.chapter_id IS NULL
        """
        params.extend([provider, model])
    else:
        sql += """
        LEFT JOIN chapter_embeddings e
          ON e.chapter_id = c.id AND e.provider = ? AND e.model = ?
        WHERE c.embed_disabled = 0
        """
        params.extend([provider, model])
    sql += " ORDER BY c.id ASC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def backfill_embeddings(provider=None, model=None, batch_size=8, limit=0, only_missing=True):
    fd = _lock_maintenance()
    try:
        cfg = embeddings_runtime_config(provider=provider, model=model)
        provider = cfg["provider"]
        model = cfg["model"]
        output_dimensionality = cfg["output_dimensionality"]
        candidates = embedding_candidates(provider=provider, model=model, limit=limit, only_missing=only_missing)
        if not candidates:
            return {"processed": 0, "provider": provider, "model": model}
        con = connect()
        processed = 0
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            texts = [embed_input_text(row) for row in batch]
            vectors = embed_texts(
                provider,
                texts,
                input_type="passage",
                model=model,
                output_dimensionality=output_dimensionality,
            )
            for row, vector, source_text in zip(batch, vectors, texts):
                upsert_embedding(con, row["id"], provider, model, source_text, vector)
                processed += 1
            con.commit()
        con.close()
        return {"processed": processed, "provider": provider, "model": model}
    finally:
        _unlock_maintenance(fd)


def semantic_search(query, limit=8, provider=None, model=None, use_binary=None,
                    shelves=None, exclude_shelves=None, tags=None, exclude_tags=None, engram_types=None):
    """Semantic search via embeddings.

    If use_binary is True (default when binary embeddings exist), does a two-pass:
    1. Hamming distance over embedding_bin (BLOB, fast popcount) → top 4*limit
    2. Rescore with float32 cosine over embedding_json on the top survivors
    Total cost is dominated by stage 2 over a small subset, so this is much
    faster than full-scan cosine on 200+ chapters.

    If embedding_bin is missing for the matched provider/model rows (legacy data
    from before the migration), falls back to full-scan float32 cosine.
    """
    cfg = embeddings_runtime_config(provider=provider, model=model)
    provider = cfg["provider"]
    model = cfg["model"]
    output_dimensionality = cfg["output_dimensionality"]
    init_db()
    query_vec = embed_texts(
        provider,
        [query],
        input_type="query",
        model=model,
        output_dimensionality=output_dimensionality,
    )[0]
    _sem_extra_clauses, _sem_extra_params = _filter_clauses_and_params(
        shelves=shelves, exclude_shelves=exclude_shelves,
        tags=tags, exclude_tags=exclude_tags,
        engram_types=engram_types,
    )
    _sem_extra_sql = (" AND " + " AND ".join(_sem_extra_clauses)) if _sem_extra_clauses else ""
    con = connect()
    rows = con.execute(
        f"""
        SELECT
          c.id,
          c.book_id,
          c.title,
          c.spr,
          c.raw,
          c.tokens,
          c.importance,
          c.created_at,
          c.updated_at,
          c.last_access,
          c.access_count,
          c.tags_json,
          b.title AS book_title,
          b.source_path,
          s.name AS shelf,
          e.embedding_json,
          e.embedding_bin,
          e.dims
        FROM chapter_embeddings e
        JOIN chapters c ON c.id = e.chapter_id
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE e.provider = ? AND e.model = ?{_sem_extra_sql}
        """,
        (provider, model, *_sem_extra_params),
    ).fetchall()
    con.close()
    if not rows:
        return []

    have_binary = all(r["embedding_bin"] is not None for r in rows)
    use_binary = have_binary if use_binary is None else (use_binary and have_binary)

    candidates = rows
    if use_binary:
        query_bin = quantize_binary(query_vec)
        binary_scored = []
        for row in rows:
            dist = hamming_distance(query_bin, row["embedding_bin"])
            binary_scored.append((dist, row))
        binary_scored.sort(key=lambda x: x[0])
        prefilter = max(limit * 4, 32)
        candidates = [row for _, row in binary_scored[:prefilter]]

    scored = []
    for row in candidates:
        data = dict(row)
        vec = json.loads(data["embedding_json"])
        data["semantic_score"] = round(cosine_similarity(query_vec, vec), 6)
        data["tags"] = json.loads(data["tags_json"] or "[]")
        data.pop("embedding_json", None)
        data.pop("embedding_bin", None)
        scored.append(data)
    scored.sort(key=lambda r: r["semantic_score"], reverse=True)
    return scored[:limit]


def pack(query, budget_tokens=4000, limit=8, threshold=0.60,
         shelves=None, exclude_shelves=None, tags=None, exclude_tags=None):
    candidates = search(query, limit=limit * 2,
                        shelves=shelves, exclude_shelves=exclude_shelves,
                        tags=tags, exclude_tags=exclude_tags)
    if not candidates or candidates[0]["score"] < threshold:
        result = {
            "query": query,
            "null_retrieval": True,
            "reason": "below_threshold",
            "threshold": threshold,
            "budget_tokens": budget_tokens,
            "items": [],
        }
        log_query(query, budget_tokens, 0, True, result)
        return result

    chosen = []
    used = 0
    for row in candidates:
        candidate_text = f"{row['title']} {row['spr']}"
        if any(overlap_ratio(candidate_text, f"{item['title']} {item['spr']}") > 0.85 for item in chosen):
            continue
        cost = token_estimate(row["spr"])
        if used + cost > budget_tokens:
            continue
        chosen.append(row)
        used += cost
        if len(chosen) >= limit:
            break

    ordered = sandwich_order(chosen)
    items = []
    for row in ordered:
        neighbors = linked_neighbors(row["id"])[:3]
        items.append(
            {
                "id": row["id"],
                "shelf": row["shelf"],
                "book_title": row["book_title"],
                "title": row["title"],
                "score": row["score"],
                "spr": row["spr"],
                "source_path": row["source_path"],
                "neighbors": neighbors,
                "citation": f"[mem:{row['id']}]",
            }
        )
        touch_access(row["id"])

    result = {
        "query": query,
        "null_retrieval": False,
        "reason": "ok",
        "threshold": threshold,
        "budget_tokens": budget_tokens,
        "used_tokens_estimate": used,
        "items": items,
    }
    log_query(query, budget_tokens, len(items), False, result)
    return result


def hybrid_pack(query, budget_tokens=4000, limit=8, threshold=0.40, provider=None, model=None,
                shelves=None, exclude_shelves=None, tags=None, exclude_tags=None,
                engram_types=None):
    cfg = embeddings_runtime_config(provider=provider, model=model)
    provider = cfg["provider"]
    model = cfg["model"]
    lexical = search(query, limit=limit * 2,
                     shelves=shelves, exclude_shelves=exclude_shelves,
                     tags=tags, exclude_tags=exclude_tags,
                     engram_types=engram_types)
    semantic = semantic_search(query, limit=limit * 2, provider=provider, model=model,
                               shelves=shelves, exclude_shelves=exclude_shelves,
                               tags=tags, exclude_tags=exclude_tags,
                               engram_types=engram_types)
    merged = {}
    for row in lexical:
        merged[row["id"]] = dict(row)
        merged[row["id"]]["lexical_score"] = row["score"]
        merged[row["id"]]["semantic_score"] = 0.0
    for row in semantic:
        if row["id"] not in merged:
            merged[row["id"]] = dict(row)
            merged[row["id"]]["lexical_score"] = 0.0
        merged[row["id"]]["semantic_score"] = row["semantic_score"]
    if not merged:
        result = {
            "query": query,
            "null_retrieval": True,
            "reason": "no_candidates",
            "threshold": threshold,
            "budget_tokens": budget_tokens,
            "items": [],
            "provider": provider,
            "model": model,
        }
        log_query(query, budget_tokens, 0, True, result)
        return result

    ranked = []
    now = now_ts()
    for row in merged.values():
        recency_days = max(0.0, (now - (row["updated_at"] or row["created_at"])) / 86400.0)
        recency = math.exp(-recency_days / 30.0)
        importance = max(0.0, min(1.0, float(row["importance"])))
        lexical_score = float(row.get("lexical_score", 0.0))
        semantic_score = float(row.get("semantic_score", 0.0))
        domain_prior = source_domain_prior(query, row)
        score = 0.10 * recency + 0.15 * importance + 0.25 * lexical_score + 0.45 * semantic_score + domain_prior
        row["score"] = round(score, 4)
        ranked.append(row)
    ranked.sort(key=lambda r: r["score"], reverse=True)

    # Cross-encoder rerank stage (Fase 1 v3.4): if FlashRank is available and
    # enabled via env, take top candidates from Park scoring and rerank by
    # query-passage relevance, then blend with the Park score (50/50).
    rerank_provider = rerank_provider_default()
    if rerank_provider == "flashrank" and ranked:
        rerank_pool = max(limit * 4, 16)
        head = ranked[:rerank_pool]
        passages = [
            {"id": str(r["id"]), "text": f"{r['title']}\n\n{r['spr']}"}
            for r in head
        ]
        reranked = flashrank_rerank(query, passages)
        if reranked and isinstance(reranked, list) and "score" in reranked[0]:
            score_by_id = {p["id"]: float(p.get("score", 0.0)) for p in reranked}
            max_rerank = max(score_by_id.values()) or 1.0
            for r in head:
                rerank_score = score_by_id.get(str(r["id"]), 0.0) / max_rerank
                r["rerank_score"] = round(rerank_score, 4)
                # Blend Park scoring + rerank 50/50 (rerank emphasizes
                # query-passage semantic match; Park brings recency/importance).
                r["score"] = round(0.5 * r["score"] + 0.5 * rerank_score, 4)
            head.sort(key=lambda r: r["score"], reverse=True)
            ranked = head + ranked[rerank_pool:]

    if not ranked or ranked[0]["score"] < threshold:
        result = {
            "query": query,
            "null_retrieval": True,
            "reason": "below_threshold",
            "threshold": threshold,
            "budget_tokens": budget_tokens,
            "items": [],
            "provider": provider,
            "model": model,
        }
        log_query(query, budget_tokens, 0, True, result)
        return result

    chosen = []
    used = 0
    for row in ranked:
        candidate_text = f"{row['title']} {row['spr']}"
        if any(overlap_ratio(candidate_text, f"{item['title']} {item['spr']}") > 0.85 for item in chosen):
            continue
        cost = token_estimate(row["spr"])
        if used + cost > budget_tokens:
            continue
        chosen.append(row)
        used += cost
        if len(chosen) >= limit:
            break

    items = []
    for row in sandwich_order(chosen):
        neighbors = linked_neighbors(row["id"])[:3]
        item = {
            "id": row["id"],
            "shelf": row["shelf"],
            "book_title": row["book_title"],
            "title": row["title"],
            "score": row["score"],
            "lexical_score": round(float(row.get("lexical_score", 0.0)), 4),
            "semantic_score": round(float(row.get("semantic_score", 0.0)), 4),
            "spr": row["spr"],
            "source_path": row["source_path"],
            "neighbors": neighbors,
            "citation": f"[mem:{row['id']}]",
        }
        if "rerank_score" in row:
            item["rerank_score"] = round(float(row["rerank_score"]), 4)
        items.append(item)
        touch_access(row["id"])

    result = {
        "query": query,
        "null_retrieval": False,
        "reason": "ok",
        "threshold": threshold,
        "budget_tokens": budget_tokens,
        "used_tokens_estimate": used,
        "items": items,
        "provider": provider,
        "model": model,
    }
    log_query(query, budget_tokens, len(items), False, result)
    return result


def engram_pack(query, budget_tokens=4000, limit=8, threshold=0.30,
                provider=None, model=None,
                shelves=None, exclude_shelves=None,
                tags=None, exclude_tags=None,
                quotas=None, k=60):
    """Reciprocal Rank Fusion (RRF) over the 3 ENGRAM buckets:
    episodic, semantic, procedural.

    Each bucket is queried independently via hybrid_pack (which already does
    lexical+semantic+rerank within bucket). Results are then fused using RRF:
        rrf_score(d) = Σ_b 1 / (k + rank_b(d))

    quotas: dict {episodic: 2, semantic: 4, procedural: 2} — minimum number
    of items from each bucket BEFORE general RRF ranking. Ensures the prompt
    always gets a balanced view (no fact-only or no-procedural collapse).
    """
    quotas = quotas or {"episodic": 2, "semantic": 4, "procedural": 2}
    buckets = ["episodic", "semantic", "procedural"]

    # Per-bucket queries
    bucket_results = {}
    for et in buckets:
        try:
            r = hybrid_pack(query, budget_tokens=budget_tokens, limit=limit * 2,
                            threshold=0.0,  # don't filter by threshold per-bucket
                            provider=provider, model=model,
                            shelves=shelves, exclude_shelves=exclude_shelves,
                            tags=tags, exclude_tags=exclude_tags,
                            engram_types=[et])
            items = r.get("items", []) if isinstance(r, dict) else []
            bucket_results[et] = items
        except Exception as e:
            bucket_results[et] = []

    # RRF scoring across buckets
    rrf_scores = {}
    item_lookup = {}
    for et, items in bucket_results.items():
        for rank_idx, item in enumerate(items):
            item_id = item.get("id") or item.get("chapter_id")
            if item_id is None:
                continue
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank_idx + 1)
            if item_id not in item_lookup:
                item_lookup[item_id] = item
                item_lookup[item_id]["engram_type"] = et

    # Apply quotas FIRST: take top N from each bucket guaranteed
    quota_picks = []
    quota_taken = set()
    for et in buckets:
        n_quota = quotas.get(et, 0)
        for item in bucket_results.get(et, [])[:n_quota]:
            iid = item.get("id") or item.get("chapter_id")
            if iid is None or iid in quota_taken:
                continue
            quota_picks.append((rrf_scores.get(iid, 0.0), item))
            quota_taken.add(iid)

    # Then fill remaining slots from RRF ranking
    remaining = []
    for iid, score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True):
        if iid in quota_taken:
            continue
        if iid in item_lookup:
            remaining.append((score, item_lookup[iid]))

    # Combine: quota_picks first (in their original RRF order), then remaining
    quota_picks.sort(key=lambda x: x[0], reverse=True)
    chosen = []
    used = 0
    for score, item in quota_picks + remaining:
        if score < threshold and len(chosen) > sum(quotas.values()):
            break
        cost = token_estimate(item.get("spr", ""))
        if used + cost > budget_tokens:
            continue
        item = dict(item)
        item["rrf_score"] = round(score, 4)
        chosen.append(item)
        used += cost
        if len(chosen) >= limit:
            break

    return {
        "query": query,
        "engram_pack": True,
        "rrf_k": k,
        "quotas": quotas,
        "buckets_size": {et: len(items) for et, items in bucket_results.items()},
        "items": chosen,
        "total_tokens": used,
        "budget_tokens": budget_tokens,
        "null_retrieval": len(chosen) == 0,
    }


def sandwich_order(rows):
    if len(rows) <= 2:
        return rows
    ordered = []
    left = 0
    right = len(rows) - 1
    while left <= right:
        ordered.append(rows[left])
        left += 1
        if left <= right:
            ordered.append(rows[right])
            right -= 1
    return ordered


def touch_access(chapter_id):
    init_db()
    con = connect()
    con.execute(
        "UPDATE chapters SET last_access=?, access_count=access_count+1 WHERE id=?",
        (now_ts(), chapter_id),
    )
    con.commit()
    con.close()


def expand(chapter_id):
    init_db()
    con = connect()
    row = con.execute(
        """
        SELECT c.*, b.title AS book_title, b.source_path, s.name AS shelf
        FROM chapters c
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE c.id=?
        """,
        (chapter_id,),
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"chapter not found: {chapter_id}")
    touch_access(chapter_id)
    data = dict(row)
    data["neighbors"] = linked_neighbors(chapter_id)
    data["tags"] = json.loads(data["tags_json"] or "[]")
    return data


def add_link(src_id, dst_id, link_type, weight=1.0, note=None):
    init_db()
    con = connect()
    con.execute(
        """
        INSERT OR REPLACE INTO chapter_links(src_chapter_id, dst_chapter_id, link_type, weight, note, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (src_id, dst_id, link_type, weight, note, now_ts()),
    )
    con.commit()
    con.close()


# --- suggest-links (v3.9.0) ---


def suggest_links(chapter_id=None, limit=8, min_score=0.0, provider=None,
                  model=None, dedup=True):
    """Compute K nearest vector neighbors and store as candidate suggestions.

    When chapter_id is given, only that chapter's neighbors are proposed.
    When None, all chapters with embeddings are processed (batch mode).

    Filters: no self-links, no already-linked pairs (either direction),
    no same-book chapters.  Already existing candidates (any status) are
    not duplicated (UNIQUE constraint).
    """
    cfg = embeddings_runtime_config(provider=provider, model=model)
    provider = cfg["provider"]
    model = cfg["model"]
    init_db()
    con = connect()

    # Gather source chapters with embeddings for this provider/model.
    src_sql = """
        SELECT DISTINCT c.id, c.title, c.book_id, c.spr
        FROM chapters c
        JOIN chapter_embeddings e ON e.chapter_id = c.id
        WHERE e.provider = ? AND e.model = ?
          AND c.embed_disabled = 0
    """
    src_params: list = [provider, model]
    if chapter_id is not None:
        src_sql += " AND c.id = ?"
        src_params.append(chapter_id)
    src_rows = con.execute(src_sql, src_params).fetchall()
    if not src_rows:
        con.close()
        return {"proposed": 0, "skipped": 0}

    # Load all vectors once for batch mode.
    vec_sql = """
        SELECT e.chapter_id, e.embedding_json
        FROM chapter_embeddings e
        JOIN chapters c ON c.id = e.chapter_id
        WHERE e.provider = ? AND e.model = ?
          AND c.embed_disabled = 0
    """
    all_vecs = {
        row["chapter_id"]: json.loads(row["embedding_json"])
        for row in con.execute(vec_sql, (provider, model)).fetchall()
    }
    if not all_vecs:
        con.close()
        return {"proposed": 0, "skipped": 0}

    proposed = 0
    skipped = 0

    for src_row in src_rows:
        src_id = src_row["id"]
        src_book = src_row["book_id"]
        src_vec = all_vecs.get(src_id)
        if src_vec is None:
            continue

        # Compute cosine similarity against all other chapters.
        scored = []
        for dst_id, dst_vec in all_vecs.items():
            if dst_id == src_id:
                continue
            sim = cosine_similarity(src_vec, dst_vec)
            if sim >= min_score:
                scored.append((dst_id, sim))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Load filters: existing links + existing suggestions + same-book
        existing_links = {
            row["id2"]
            for row in con.execute(
                "SELECT src_chapter_id AS id2 FROM chapter_links WHERE dst_chapter_id=? "
                "UNION SELECT dst_chapter_id AS id2 FROM chapter_links WHERE src_chapter_id=?",
                (src_id, src_id),
            ).fetchall()
        }
        existing_suggestions = {
            row["id2"]
            for row in con.execute(
                "SELECT src_chapter_id AS id2 FROM link_suggestions WHERE dst_chapter_id=? "
                "UNION SELECT dst_chapter_id AS id2 FROM link_suggestions WHERE src_chapter_id=?",
                (src_id, src_id),
            ).fetchall()
        }
        candidates_added = 0
        for dst_id, sim in scored:
            if candidates_added >= limit:
                break
            if dst_id in existing_links or dst_id in existing_suggestions:
                skipped += 1
                continue
            # Check same-book
            dst_book = con.execute(
                "SELECT book_id FROM chapters WHERE id=?", (dst_id,)
            ).fetchone()
            if dst_book and dst_book["book_id"] == src_book:
                skipped += 1
                continue
            # Insert suggestion
            try:
                con.execute(
                    """INSERT INTO link_suggestions
                       (src_chapter_id, dst_chapter_id, score, status, created_at)
                       VALUES (?, ?, ?, 'candidate', ?)""",
                    (src_id, dst_id, round(sim, 6), now_ts()),
                )
                proposed += 1
                candidates_added += 1
            except sqlite3.IntegrityError:
                skipped += 1

    con.commit()
    con.close()
    return {"proposed": proposed, "skipped": skipped, "provider": provider, "model": model}


def list_link_suggestions(status=None, limit=50):
    """List link suggestions with both chapters' context.

    Status filter: None = all, or 'candidate'/'accepted'/'rejected'.
    """
    init_db()
    con = connect()
    sql = """
        SELECT
          ls.id, ls.src_chapter_id, ls.dst_chapter_id, ls.score,
          ls.status, ls.created_at, ls.reviewed_at, ls.reviewer_note,
          sc.title AS src_title, sc.spr AS src_spr,
          dc.title AS dst_title, dc.spr AS dst_spr,
          sb.title AS src_book, dbk.title AS dst_book
        FROM link_suggestions ls
        JOIN chapters sc ON sc.id = ls.src_chapter_id
        JOIN chapters dc ON dc.id = ls.dst_chapter_id
        JOIN books sb ON sb.id = sc.book_id
        JOIN books dbk ON dbk.id = dc.book_id
    """
    params: list = []
    if status:
        sql += " WHERE ls.status = ?"
        params.append(status)
    sql += " ORDER BY ls.score DESC, ls.id ASC LIMIT ?"
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def review_link_suggestion(suggestion_id, action, note=None):
    """Accept or reject a link suggestion.

    'accept' creates a real chapter_links edge (link_type='suggested',
    weight=score) and marks the suggestion accepted.
    'reject' marks it rejected so it won't be re-proposed.
    """
    if action not in ("accept", "reject"):
        raise SystemExit(f"review_link_suggestion: action must be 'accept' or 'reject', got {action!r}")
    init_db()
    con = connect()
    row = con.execute(
        "SELECT * FROM link_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"suggestion not found: {suggestion_id}")
    if row["status"] != "candidate":
        con.close()
        raise SystemExit(
            f"suggestion {suggestion_id} is already {row['status']} (cannot {action})"
        )

    if action == "accept":
        con.execute(
            """INSERT OR REPLACE INTO chapter_links
               (src_chapter_id, dst_chapter_id, link_type, weight, note, created_at)
               VALUES (?, ?, 'suggested', ?, ?, ?)""",
            (row["src_chapter_id"], row["dst_chapter_id"], row["score"], note, now_ts()),
        )
    con.execute(
        "UPDATE link_suggestions SET status=?, reviewed_at=?, reviewer_note=? WHERE id=?",
        (action, now_ts(), note, suggestion_id),
    )
    con.commit()
    con.close()
    return {"suggestion_id": suggestion_id, "action": action, "status": action}


def update_chapter(chapter_id, content=None, title=None, tags=None, importance=None):
    """Update a chapter in place (v3.8.0+).

    Only the fields explicitly passed are changed; the rest are preserved.
    When the embedded surface changes (content or title), all stored
    embeddings for the chapter are dropped so the next embed-backfill
    recomputes them from the new text — stale vectors are worse than
    missing ones because they keep ranking the old content.

    FTS is kept consistent via the contentless-table delete+insert pair.
    When `title` changes, the parent book's title/slug are kept in sync;
    a slug collision with another book aborts with a clear error.

    Returns a small report dict.
    """
    if content is None and title is None and tags is None and importance is None:
        raise SystemExit("update_chapter: nothing to update (pass content, title, tags, and/or importance)")
    init_db()
    con = connect()
    row = con.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"chapter not found: {chapter_id}")
    old = dict(row)

    new_raw = normalize_text(content) if content is not None else old["raw"]
    new_title = title if title is not None else old["title"]
    new_tags = list(tags) if tags is not None else json.loads(old["tags_json"] or "[]")
    new_importance = float(importance) if importance is not None else old["importance"]
    new_spr = simple_spr(new_raw)
    content_changed = (new_raw != old["raw"]) or (new_title != old["title"])

    # v3.9.0 — re-scan content for secrets on content change
    embed_disabled_new = old.get("embed_disabled", 0)
    embed_disable_reason_new = old.get("embed_disable_reason")
    if content_changed and scan_content_for_secrets:
        secret_reason = scan_content_for_secrets(new_raw)
        if secret_reason:
            embed_disabled_new = 1
            embed_disable_reason_new = secret_reason

    if title is not None and title != old["title"]:
        new_slug = slugify(title)
        collision = con.execute(
            "SELECT id FROM books WHERE shelf_id=(SELECT shelf_id FROM books WHERE id=?) AND slug=? AND id != ?",
            (old["book_id"], new_slug, old["book_id"]),
        ).fetchone()
        if collision:
            con.close()
            raise SystemExit(
                f"update_chapter: title slug '{new_slug}' already used by book {collision['id']} "
                f"on the same shelf; choose a different title"
            )
        con.execute(
            "UPDATE books SET title=?, slug=?, updated_at=? WHERE id=?",
            (new_title, new_slug, now_ts(), old["book_id"]),
        )

    delete_chapter_fts(con, old)
    con.execute(
        """
        UPDATE chapters SET title=?, spr=?, raw=?, tokens=?, importance=?, updated_at=?, tags_json=?, embed_disabled=?, embed_disable_reason=?
        WHERE id=?
        """,
        (
            new_title,
            new_spr,
            new_raw,
            token_estimate(new_raw),
            new_importance,
            now_ts(),
            json.dumps(new_tags),
            embed_disabled_new,
            embed_disable_reason_new,
            chapter_id,
        ),
    )
    insert_chapter_fts(con, chapter_id, new_title, new_spr, new_raw, json.dumps(new_tags))

    # The book container reflects the last content mutation, consistent with
    # upsert_book() bumping updated_at on every add_text.
    con.execute(
        "UPDATE books SET updated_at=? WHERE id=?",
        (now_ts(), old["book_id"]),
    )

    embeddings_dropped = 0
    if content_changed:
        cur = con.execute("DELETE FROM chapter_embeddings WHERE chapter_id=?", (chapter_id,))
        embeddings_dropped = cur.rowcount

    con.commit()
    con.close()
    return {
        "chapter_id": chapter_id,
        "content_changed": content_changed,
        "embeddings_dropped": embeddings_dropped,
        "title": new_title,
        "tags": new_tags,
        "importance": new_importance,
        "embed_disabled": embed_disabled_new,
        "embed_disable_reason": embed_disable_reason_new,
    }


def delete_chapter(chapter_id, prune_book=True):
    """Delete a chapter and its dependent rows (v3.8.0+).

    chapter_embeddings and chapter_links are removed by the ON DELETE
    CASCADE declared in the schema (connect() enables PRAGMA foreign_keys).
    The FTS row needs the explicit contentless-table delete. When the parent
    book is left without chapters it is pruned too (disable with
    prune_book=False).

    The report includes title + sha256 of the deleted raw content so the
    caller can archive it before or after the fact if needed.
    """
    init_db()
    con = connect()
    row = con.execute(
        "SELECT id, book_id, title, spr, raw, tags_json FROM chapters WHERE id=?",
        (chapter_id,),
    ).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"chapter not found: {chapter_id}")

    title = row["title"]
    raw_sha256 = text_hash(row["raw"] or "")
    embeddings_removed = con.execute(
        "SELECT COUNT(*) FROM chapter_embeddings WHERE chapter_id=?", (chapter_id,)
    ).fetchone()[0]
    links_removed = con.execute(
        "SELECT COUNT(*) FROM chapter_links WHERE src_chapter_id=? OR dst_chapter_id=?",
        (chapter_id, chapter_id),
    ).fetchone()[0]

    delete_chapter_fts(con, row)
    con.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))

    book_deleted = False
    remaining = con.execute(
        "SELECT COUNT(*) FROM chapters WHERE book_id=?", (row["book_id"],)
    ).fetchone()[0]
    if prune_book and remaining == 0:
        con.execute("DELETE FROM books WHERE id=?", (row["book_id"],))
        book_deleted = True

    con.commit()
    con.close()
    return {
        "chapter_id": chapter_id,
        "title": title,
        "raw_sha256": raw_sha256,
        "embeddings_removed": embeddings_removed,
        "links_removed": links_removed,
        "book_deleted": book_deleted,
    }


def log_query(query_text, budget_tokens, result_count, null_retrieval, details):
    init_db()
    con = connect()
    con.execute(
        """
        INSERT INTO queries_log(query_text, budget_tokens, result_count, null_retrieval, details_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (query_text, budget_tokens, result_count, int(null_retrieval), json.dumps(details), now_ts()),
    )
    con.commit()
    con.close()


def stats():
    init_db()
    con = connect()
    out = {
        "db_path": str(DB_PATH),
        "shelves": con.execute("SELECT COUNT(*) FROM shelves").fetchone()[0],
        "books": con.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "chapters": con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
        "embeddings": con.execute("SELECT COUNT(*) FROM chapter_embeddings").fetchone()[0],
        "links": con.execute("SELECT COUNT(*) FROM chapter_links").fetchone()[0],
        "suggestions": con.execute(
            "SELECT COUNT(*) FROM link_suggestions WHERE status='candidate'"
        ).fetchone()[0],
        "suggestions_total": con.execute("SELECT COUNT(*) FROM link_suggestions").fetchone()[0],
        "queries": con.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0],
        "embed_disabled": con.execute(
            "SELECT COUNT(*) FROM chapters WHERE embed_disabled=1"
        ).fetchone()[0],
        "embed_disabled_by_reason": [
            dict(row)
            for row in con.execute(
                """
                SELECT embed_disable_reason AS reason, COUNT(*) AS count
                FROM chapters
                WHERE embed_disabled=1 AND embed_disable_reason IS NOT NULL
                GROUP BY embed_disable_reason
                ORDER BY count DESC
                """
            ).fetchall()
        ],
        "embedding_sets": [
            dict(row)
            for row in con.execute(
                """
                SELECT provider, model, COUNT(*) AS count
                FROM chapter_embeddings
                GROUP BY provider, model
                ORDER BY count DESC, provider ASC, model ASC
                """
            ).fetchall()
        ],
    }
    con.close()
    return out


def bootstrap():
    fd = _lock_maintenance()
    try:
        if DB_PATH.exists():
            DB_PATH.unlink()
        init_db()
        bootstrap_docs = load_bootstrap_docs()
        loaded = []
        for path, shelf, title, tags in bootstrap_docs:
            if not Path(path).exists():
                continue
            chapter_id = add_file(path, shelf_name=shelf, title=title, tags=tags, importance=0.8)
            loaded.append((path, chapter_id))

        title_to_id = {}
        for _, _, title, _ in bootstrap_docs:
            res = search(title, limit=1)
            if res:
                title_to_id[title] = res[0]["id"]

        def maybe_link(src_title, dst_title, link_type):
            src = title_to_id.get(src_title)
            dst = title_to_id.get(dst_title)
            if src and dst:
                add_link(src, dst, link_type)

        maybe_link("openclaw-roadmap", "openclaw-master-plan", "related_to")
        maybe_link("openclaw-master-plan", "openclaw-architecture", "depends_on")
        maybe_link("hermes-soul", "hermes-memory-stable", "anchors")
        maybe_link("hermes-user-profile", "hermes-memory-stable", "related_to")

        return loaded
    finally:
        _unlock_maintenance(fd)


def parse_tags(text):
    if not text:
        return []
    return [chunk.strip() for chunk in text.split(",") if chunk.strip()]



# --- doctor (v3.2+): audit plugin layout for sibling collisions ---

import re as _re_doctor


def _parse_plugin_yaml(path: Path) -> dict:
    """Extract the minimum we need from plugin.yaml without requiring PyYAML.
    Returns a dict with keys: name, version, provides_hooks (list). Any missing
    field becomes None / empty list. Multi-line or nested YAML beyond the flat
    keys above is not attempted — plugin.yaml in the kit format is flat enough.
    """
    out = {"name": None, "version": None, "provides_hooks": []}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return out
    in_hooks = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if in_hooks and line and not line.startswith((" ", "\t", "-")):
                in_hooks = False
            continue
        if in_hooks:
            if line.startswith("-") or line.startswith(("  -", " -", "\t-")):
                item = stripped.lstrip("-").strip().strip("\"\'")
                if item:
                    out["provides_hooks"].append(item)
                continue
            else:
                in_hooks = False
        m = _re_doctor.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", stripped)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        val = val.strip("\"\'")
        if key == "name" and val:
            out["name"] = val
        elif key == "version" and val:
            out["version"] = val
        elif key == "provides_hooks":
            if val:
                out["provides_hooks"] = [x.strip().strip("\"\'[]") for x in val.split(",") if x.strip()]
            else:
                in_hooks = True
    return out


def _doctor_hermes_home() -> Path | None:
    """Resolve HERMES_HOME from env cascade. Returns None if not set."""
    for key in ("HMK_HERMES_HOME", "HERMES_HOME"):
        v = os.environ.get(key)
        if v:
            return Path(v)
    return None


def doctor() -> dict:
    """Audit the Hermes plugin layout for common foot-guns.

    Detects:
      - Sibling collisions: two or more plugin dirs with the same `name:` in
        their plugin.yaml. Hermes loads ALL of them, and the last writer wins
        on hooks — silent source of state corruption.
      - Suspicious suffixes: directories under plugins/ matching .bak/.old/
        .prev/.vNN. These belong under plugin-backups/ — sibling placement
        causes the backup to register as an active plugin.

    Output: JSON with keys:
      ok: bool  (False if any issue found)
      hermes_home: resolved HERMES_HOME path (or null)
      plugins_dir: scanned directory (or null)
      plugins: list of dicts (dir_name, name, version, hooks)
      issues: list of dicts (kind, detail, dirs)
    """
    out = {
        "ok": True,
        "hermes_home": None,
        "plugins_dir": None,
        "plugins": [],
        "issues": [],
    }
    hh = _doctor_hermes_home()
    if not hh or not hh.exists():
        out["ok"] = False
        out["issues"].append({
            "kind": "no_hermes_home",
            "detail": "HMK_HERMES_HOME / HERMES_HOME not set or directory does not exist",
            "dirs": [],
        })
        return out
    out["hermes_home"] = str(hh)
    plugins_dir = hh / "plugins"
    if not plugins_dir.exists():
        out["ok"] = False
        out["issues"].append({
            "kind": "no_plugins_dir",
            "detail": f"{plugins_dir} does not exist",
            "dirs": [],
        })
        return out
    out["plugins_dir"] = str(plugins_dir)

    # Collect loadable plugins (anything with a valid plugin.yaml)
    by_name: dict = {}
    suspect = []
    SUSPECT_RE = _re_doctor.compile(r"\.(bak|old|prev|orig)(\.[a-zA-Z0-9_-]+)?$|\.v\d+(\.\d+)*(\.bak)?$")
    for child in sorted(plugins_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "plugin.yaml"
        if not manifest.exists():
            continue
        meta = _parse_plugin_yaml(manifest)
        entry = {
            "dir_name": child.name,
            "name": meta["name"],
            "version": meta["version"],
            "hooks": meta["provides_hooks"],
        }
        out["plugins"].append(entry)
        if meta["name"]:
            by_name.setdefault(meta["name"], []).append(child.name)
        if SUSPECT_RE.search(child.name):
            suspect.append(child.name)

    # Name collisions
    for name, dirs in by_name.items():
        if len(dirs) > 1:
            out["ok"] = False
            out["issues"].append({
                "kind": "name_collision",
                "detail": (
                    f"multiple plugin directories declare name={name!r}. "
                    "Hermes loads all of them and their hooks run in parallel; "
                    "the last writer wins. Move stale copies to plugin-backups/."
                ),
                "dirs": dirs,
            })

    # Suspect directory suffixes
    if suspect:
        out["ok"] = False
        out["issues"].append({
            "kind": "suspect_suffix",
            "detail": (
                "directory names match backup/old patterns. Even if unreferenced "
                "in config.yaml, Hermes will load them if they contain a valid "
                "plugin.yaml. Move to plugin-backups/ to silence."
            ),
            "dirs": suspect,
        })

    return out


def main():
    parser = argparse.ArgumentParser(description="Local memory control for Hermes")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("bootstrap")
    sub.add_parser("stats")
    sub.add_parser("embed-config")

    p_add_text = sub.add_parser("add-text")
    p_add_text.add_argument("--shelf", required=True, choices=sorted(DEFAULT_SHELVES))
    p_add_text.add_argument("--title", required=True)
    p_add_text.add_argument("--raw", required=True)
    p_add_text.add_argument("--tags", default="")
    p_add_text.add_argument("--importance", type=float, default=0.5)

    p_add_file = sub.add_parser("add-file")
    p_add_file.add_argument("--shelf", required=True, choices=sorted(DEFAULT_SHELVES))
    p_add_file.add_argument("--path", required=True)
    p_add_file.add_argument("--title")
    p_add_file.add_argument("--tags", default="")
    p_add_file.add_argument("--importance", type=float, default=0.5)

    def _add_filter_args(p):
        """Add --shelf/--exclude-shelf/--tag/--exclude-tag (CSV) to a subparser.
        Used on retrieval commands. Default empty = no filter (back-compat)."""
        p.add_argument("--shelf", default="", help="CSV of shelves to include (e.g. plans,evidence,mc-episodic)")
        p.add_argument("--exclude-shelf", default="", help="CSV of shelves to exclude")
        p.add_argument("--tag", default="", help="CSV of tags to require (any-match via JSON1 json_each)")
        p.add_argument("--exclude-tag", default="", help="CSV of tags to exclude")

    p_search = sub.add_parser("search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=8)
    _add_filter_args(p_search)

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--query", required=True)
    p_pack.add_argument("--budget", type=int, default=4000)
    p_pack.add_argument("--limit", type=int, default=8)
    p_pack.add_argument("--threshold", type=float, default=0.60)
    _add_filter_args(p_pack)

    p_expand = sub.add_parser("expand")
    p_expand.add_argument("--id", type=int, required=True)

    p_update = sub.add_parser("update", help="update a chapter in place (content/title/tags/importance)")
    p_update.add_argument("--id", type=int, required=True)
    p_update.add_argument("--raw", help="new content (recomputed spr; drops stored embeddings)")
    p_update.add_argument("--title", help="new title (keeps book title/slug in sync)")
    p_update.add_argument("--tags", help="CSV of tags; replaces the tag set when provided")
    p_update.add_argument("--importance", type=float)

    p_delete = sub.add_parser("delete", help="delete a chapter (cascades embeddings/links; prunes empty book)")
    p_delete.add_argument("--id", type=int, required=True)
    p_delete.add_argument("--keep-book", action="store_true", help="keep the parent book even if left empty")

    p_link = sub.add_parser("link")
    p_link.add_argument("--src", type=int, required=True)
    p_link.add_argument("--dst", type=int, required=True)
    p_link.add_argument("--type", required=True)
    p_link.add_argument("--weight", type=float, default=1.0)
    p_link.add_argument("--note")

    p_suggest = sub.add_parser("suggest-links", help="discover link candidates via vector similarity")
    p_suggest.add_argument("--chapter-id", type=int, help="suggest links for a single chapter (omit for all)")
    p_suggest.add_argument("--limit", type=int, default=8, help="max suggestions per chapter (default 8)")
    p_suggest.add_argument("--min-score", type=float, default=0.0)
    p_suggest.add_argument("--provider", default=default_embed_provider())
    p_suggest.add_argument("--model")

    p_review = sub.add_parser("review-links", help="review link suggestions (list/accept/reject)")
    p_review.add_argument("--status", help="filter by status (candidate/accepted/rejected)")
    p_review.add_argument("--limit", type=int, default=50, help="max suggestions to list")
    p_review.add_argument("--accept", type=int, help="accept suggestion by id")
    p_review.add_argument("--reject", type=int, help="reject suggestion by id")
    p_review.add_argument("--note", help="reviewer note (for accept/reject)")

    p_embed = sub.add_parser("embed-backfill")
    p_embed.add_argument("--provider", default=default_embed_provider())
    p_embed.add_argument("--model")
    p_embed.add_argument("--batch-size", type=int, default=8)
    p_embed.add_argument("--limit", type=int, default=0)
    p_embed.add_argument("--all", action="store_true", help="recompute even if embeddings already exist")

    p_sem = sub.add_parser("semantic-search")
    p_sem.add_argument("--query", required=True)
    p_sem.add_argument("--limit", type=int, default=8)
    p_sem.add_argument("--provider", default=default_embed_provider())
    p_sem.add_argument("--model")
    _add_filter_args(p_sem)

    p_hybrid = sub.add_parser("hybrid-pack")
    p_hybrid.add_argument("--query", required=True)
    p_hybrid.add_argument("--budget", type=int, default=4000)
    p_hybrid.add_argument("--limit", type=int, default=8)
    p_hybrid.add_argument("--threshold", type=float, default=0.40)
    p_hybrid.add_argument("--provider", default=default_embed_provider())
    p_hybrid.add_argument("--model")
    _add_filter_args(p_hybrid)

    p_engram = sub.add_parser("engram-pack", help="RRF over episodic+semantic+procedural buckets")
    p_engram.add_argument("--query", required=True)
    p_engram.add_argument("--budget", type=int, default=4000)
    p_engram.add_argument("--limit", type=int, default=8)
    p_engram.add_argument("--threshold", type=float, default=0.30)
    p_engram.add_argument("--provider", default=default_embed_provider())
    p_engram.add_argument("--model")
    p_engram.add_argument("--rrf-k", type=int, default=60, help="RRF k parameter")
    p_engram.add_argument("--quota-episodic", type=int, default=2)
    p_engram.add_argument("--quota-semantic", type=int, default=4)
    p_engram.add_argument("--quota-procedural", type=int, default=2)
    _add_filter_args(p_engram)

    sub.add_parser("doctor", help="audit HERMES_HOME/plugins/ for sibling collisions + suspect suffixes")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print(json.dumps({"ok": True, "db_path": str(DB_PATH)}, indent=2))
    elif args.command == "bootstrap":
        loaded = bootstrap()
        print(json.dumps({"ok": True, "loaded": loaded, "stats": stats()}, indent=2))
    elif args.command == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.command == "embed-config":
        print(json.dumps(embeddings_capabilities(), indent=2, ensure_ascii=False))
    elif args.command == "add-text":
        cid = add_text(
            shelf_name=args.shelf,
            title=args.title,
            raw=args.raw,
            tags=parse_tags(args.tags),
            importance=args.importance,
        )
        print(json.dumps({"ok": True, "chapter_id": cid}, indent=2))
    elif args.command == "add-file":
        cid = add_file(
            path=args.path,
            shelf_name=args.shelf,
            title=args.title,
            tags=parse_tags(args.tags),
            importance=args.importance,
        )
        print(json.dumps({"ok": True, "chapter_id": cid}, indent=2))
    elif args.command == "search":
        print(json.dumps(search(
            args.query, limit=args.limit,
            shelves=args.shelf, exclude_shelves=args.exclude_shelf,
            tags=args.tag, exclude_tags=args.exclude_tag,
        ), indent=2))
    elif args.command == "pack":
        print(json.dumps(pack(
            args.query, budget_tokens=args.budget, limit=args.limit, threshold=args.threshold,
            shelves=args.shelf, exclude_shelves=args.exclude_shelf,
            tags=args.tag, exclude_tags=args.exclude_tag,
        ), indent=2))
    elif args.command == "expand":
        print(json.dumps(expand(args.id), indent=2))
    elif args.command == "update":
        result = update_chapter(
            args.id,
            content=args.raw,
            title=args.title,
            tags=parse_tags(args.tags) if args.tags is not None else None,
            importance=args.importance,
        )
        print(json.dumps({"ok": True, **result}, indent=2, ensure_ascii=False))
    elif args.command == "delete":
        result = delete_chapter(args.id, prune_book=not args.keep_book)
        print(json.dumps({"ok": True, **result}, indent=2, ensure_ascii=False))
    elif args.command == "link":
        add_link(args.src, args.dst, args.type, args.weight, args.note)
        print(json.dumps({"ok": True}, indent=2))
    elif args.command == "suggest-links":
        result = suggest_links(
            chapter_id=args.chapter_id,
            limit=args.limit,
            min_score=args.min_score,
            provider=args.provider,
            model=args.model,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "review-links":
        if args.accept:
            result = review_link_suggestion(args.accept, "accept", args.note)
        elif args.reject:
            result = review_link_suggestion(args.reject, "reject", args.note)
        else:
            result = list_link_suggestions(status=args.status, limit=args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif args.command == "embed-backfill":
        result = backfill_embeddings(
            provider=args.provider,
            model=args.model,
            batch_size=args.batch_size,
            limit=args.limit,
            only_missing=not args.all,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "semantic-search":
        print(json.dumps(semantic_search(
            args.query, limit=args.limit, provider=args.provider, model=args.model,
            shelves=args.shelf, exclude_shelves=args.exclude_shelf,
            tags=args.tag, exclude_tags=args.exclude_tag,
        ), indent=2, ensure_ascii=False))
    elif args.command == "hybrid-pack":
        print(
            json.dumps(
                hybrid_pack(
                    args.query,
                    budget_tokens=args.budget,
                    limit=args.limit,
                    threshold=args.threshold,
                    provider=args.provider,
                    model=args.model,
                    shelves=args.shelf,
                    exclude_shelves=args.exclude_shelf,
                    tags=args.tag,
                    exclude_tags=args.exclude_tag,
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "engram-pack":
        print(
            json.dumps(
                engram_pack(
                    args.query,
                    budget_tokens=args.budget,
                    limit=args.limit,
                    threshold=args.threshold,
                    provider=args.provider,
                    model=args.model,
                    shelves=args.shelf,
                    exclude_shelves=args.exclude_shelf,
                    tags=args.tag,
                    exclude_tags=args.exclude_tag,
                    quotas={
                        "episodic": args.quota_episodic,
                        "semantic": args.quota_semantic,
                        "procedural": args.quota_procedural,
                    },
                    k=args.rrf_k,
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "doctor":
        result = doctor()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)
    else:
        raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
