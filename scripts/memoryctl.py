#!/usr/bin/env python3
import argparse
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
DEFAULT_EMBED_PROVIDER = "nvidia"
DEFAULT_EMBED_MODELS = {
    "nvidia": "nvidia/llama-3.2-nemoretriever-300m-embed-v1",
    "google": "gemini-embedding-001",
    "local": "sentence-transformers/all-MiniLM-L6-v2",
}
DEFAULT_EMBED_OUTPUT_DIMS = {
    "google": 768,
}
LOCAL_MODEL_CACHE = {}
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
}

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
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (chapter_id, provider, model)
        );

        CREATE INDEX IF NOT EXISTS idx_chapter_embeddings_provider_model
        ON chapter_embeddings(provider, model);
        """
    )
    migrate_embedding_table(con)
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
    cur = con.execute(
        """
        INSERT INTO chapters(book_id, ordinal, title, spr, raw, tokens, importance, created_at, updated_at, tags_json)
        VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    chapter_id = cur.lastrowid
    insert_chapter_fts(con, chapter_id, title, spr, raw, json.dumps(tags))
    con.commit()
    con.close()
    return chapter_id


def add_file(path, shelf_name, title=None, tags=None, importance=0.5, replace=True):
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    return add_text(
        shelf_name=shelf_name,
        title=title or p.stem,
        raw=raw,
        tags=tags or [],
        importance=importance,
        source_path=str(p),
        source_kind="file",
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
    raise SystemExit(f"unsupported embedding provider: {provider}")


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
                "configured": bool(
                    importlib.util.find_spec("sentence_transformers")
                    or importlib.util.find_spec("sentence_transformers")
                ),
                "default_model": default_embed_model("local"),
            },
        },
    }


def upsert_embedding(con, chapter_id, provider, model, source_text, vector):
    now = now_ts()
    con.execute(
        """
        INSERT INTO chapter_embeddings(chapter_id, provider, model, input_text_hash, dims, embedding_json, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chapter_id, provider, model) DO UPDATE SET
          provider=excluded.provider,
          model=excluded.model,
          input_text_hash=excluded.input_text_hash,
          dims=excluded.dims,
          embedding_json=excluded.embedding_json,
          updated_at=excluded.updated_at
        """,
        (
            chapter_id,
            provider,
            model,
            text_hash(source_text),
            len(vector),
            json.dumps(vector),
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


def search(query, limit=12):
    init_db()
    con = connect()
    q = fts_query_string(query)
    rows = con.execute(
        """
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
        WHERE chapters_fts MATCH ?
        LIMIT ?
        """,
        (q, limit * 3),
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
        WHERE e.chapter_id IS NULL
        """
        params.extend([provider, model])
    else:
        sql += """
        LEFT JOIN chapter_embeddings e
          ON e.chapter_id = c.id AND e.provider = ? AND e.model = ?
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


def semantic_search(query, limit=8, provider=None, model=None):
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
    con = connect()
    rows = con.execute(
        """
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
          e.embedding_json
        FROM chapter_embeddings e
        JOIN chapters c ON c.id = e.chapter_id
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE e.provider = ? AND e.model = ?
        """,
        (provider, model),
    ).fetchall()
    con.close()
    scored = []
    for row in rows:
        data = dict(row)
        vec = json.loads(data["embedding_json"])
        data["semantic_score"] = round(cosine_similarity(query_vec, vec), 6)
        data["tags"] = json.loads(data["tags_json"] or "[]")
        data.pop("embedding_json", None)
        scored.append(data)
    scored.sort(key=lambda r: r["semantic_score"], reverse=True)
    return scored[:limit]


def pack(query, budget_tokens=4000, limit=8, threshold=0.60):
    candidates = search(query, limit=limit * 2)
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


def hybrid_pack(query, budget_tokens=4000, limit=8, threshold=0.40, provider=None, model=None):
    cfg = embeddings_runtime_config(provider=provider, model=model)
    provider = cfg["provider"]
    model = cfg["model"]
    lexical = search(query, limit=limit * 2)
    semantic = semantic_search(query, limit=limit * 2, provider=provider, model=model)
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
        items.append(
            {
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
        "provider": provider,
        "model": model,
    }
    log_query(query, budget_tokens, len(items), False, result)
    return result


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
        "queries": con.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0],
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

    p_search = sub.add_parser("search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=8)

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--query", required=True)
    p_pack.add_argument("--budget", type=int, default=4000)
    p_pack.add_argument("--limit", type=int, default=8)
    p_pack.add_argument("--threshold", type=float, default=0.60)

    p_expand = sub.add_parser("expand")
    p_expand.add_argument("--id", type=int, required=True)

    p_link = sub.add_parser("link")
    p_link.add_argument("--src", type=int, required=True)
    p_link.add_argument("--dst", type=int, required=True)
    p_link.add_argument("--type", required=True)
    p_link.add_argument("--weight", type=float, default=1.0)
    p_link.add_argument("--note")

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

    p_hybrid = sub.add_parser("hybrid-pack")
    p_hybrid.add_argument("--query", required=True)
    p_hybrid.add_argument("--budget", type=int, default=4000)
    p_hybrid.add_argument("--limit", type=int, default=8)
    p_hybrid.add_argument("--threshold", type=float, default=0.40)
    p_hybrid.add_argument("--provider", default=default_embed_provider())
    p_hybrid.add_argument("--model")

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
        print(json.dumps(search(args.query, limit=args.limit), indent=2))
    elif args.command == "pack":
        print(json.dumps(pack(args.query, budget_tokens=args.budget, limit=args.limit, threshold=args.threshold), indent=2))
    elif args.command == "expand":
        print(json.dumps(expand(args.id), indent=2))
    elif args.command == "link":
        add_link(args.src, args.dst, args.type, args.weight, args.note)
        print(json.dumps({"ok": True}, indent=2))
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
        print(json.dumps(semantic_search(args.query, limit=args.limit, provider=args.provider, model=args.model), indent=2, ensure_ascii=False))
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
