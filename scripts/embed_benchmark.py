#!/usr/bin/env python3
"""Benchmark harness for embedding providers.

Runs both `semantic-search` and `hybrid-pack` on each query from an input
file, records latency + top-k + scores + null-retrieval, and (if queries
are annotated with `expected: id1,id2,id3`) computes precision@k,
recall@k, and hit@k.

Invoke via the hmk wrapper so .env is loaded and paths are absolutized:
    ./scripts/hmk embed_benchmark.py \\
        --queries $HOME/hmk-benchmarks/queries.txt \\
        --provider nvidia \\
        --model "nvidia/llama-3.2-nemoretriever-300m-embed-v1"

Default output: $HOME/hmk-benchmarks/<timestamp>-<provider>/report.{json,md}
(outside any repo, safe from accidental commits).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# --- preflight ---------------------------------------------------------

def preflight(provider: str) -> Dict[str, Any]:
    """Provider-aware key + dims check. Fails loudly on missing requirements."""
    dims = os.environ.get("HERMES_EMBED_GOOGLE_OUTPUT_DIMS")
    if provider == "google":
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            sys.exit(
                "ERROR: provider=google requires GEMINI_API_KEY or GOOGLE_API_KEY in .env\n"
                "  obtain one at https://aistudio.google.com/app/apikey"
            )
        if not dims:
            print("WARN: HERMES_EMBED_GOOGLE_OUTPUT_DIMS not set — memoryctl default applies")
    elif provider == "nvidia":
        if not os.environ.get("NVIDIA_API_KEY"):
            sys.exit("ERROR: provider=nvidia requires NVIDIA_API_KEY in .env")
    elif provider == "local":
        pass  # no keys needed
    else:
        sys.exit(f"ERROR: unknown provider '{provider}' (use nvidia|google|local)")

    info = {
        "provider": provider,
        "cwd": os.getcwd(),
        "google_dims_env": dims,
        "db_path": os.environ.get("HMK_DB_PATH") or os.environ.get("HERMES_DB_PATH", "agent-memory/library.db"),
    }
    print(f"preflight OK: {json.dumps(info, indent=None)}", file=sys.stderr)
    return info


# --- query parsing -----------------------------------------------------

def parse_queries(path: pathlib.Path) -> List[Dict[str, Any]]:
    """Parse queries file. Format:
        query text | expected: id1,id2,id3
    Lines starting with # are comments. Empty expected = negative control.
    """
    out: List[Dict[str, Any]] = []
    if not path.exists():
        sys.exit(f"ERROR: queries file not found: {path}")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        q: Dict[str, Any]
        if "|" in line and "expected:" in line:
            query_part, exp_part = line.split("|", 1)
            query = query_part.strip()
            exp_str = exp_part.split("expected:", 1)[1].strip()
            if exp_str:
                try:
                    expected = [int(x.strip()) for x in exp_str.split(",") if x.strip()]
                except ValueError:
                    expected = []
            else:
                expected = []  # negative control
            q = {"query": query, "expected": expected, "has_expected_annotation": True}
        else:
            q = {"query": line, "expected": [], "has_expected_annotation": False}
        out.append(q)
    if not out:
        sys.exit(f"ERROR: no queries found in {path}")
    return out


# --- storage info via SQL ----------------------------------------------

def storage_info(db_path: str) -> List[Dict[str, Any]]:
    """Return per-(provider, model, dims) storage numbers from chapter_embeddings."""
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT provider, model, dims,
                   COUNT(*) AS n,
                   SUM(LENGTH(embedding_json)) AS total_json_bytes,
                   AVG(LENGTH(embedding_json)) AS avg_row_bytes,
                   dims * 4 AS binary_bytes_per_row,
                   COUNT(*) * dims * 4 AS binary_total_bytes
            FROM chapter_embeddings
            GROUP BY provider, model, dims
            """
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            out.append({
                "provider": r[0],
                "model": r[1],
                "dims": r[2],
                "count": r[3],
                "total_json_bytes": r[4],
                "avg_row_bytes": round(r[5] or 0, 1),
                "binary_bytes_per_row": r[6],
                "binary_total_bytes": r[7],
            })
        return out
    except sqlite3.Error as e:
        print(f"WARN: could not read storage info: {e}", file=sys.stderr)
        return []


# --- run memoryctl subcommand -----------------------------------------

def run_memoryctl(subcmd: str, args: List[str]) -> Tuple[Any, float]:
    """Invoke memoryctl.py <subcmd> --json-like via subprocess. Returns (parsed_json, elapsed_s)."""
    cmd = ["python3", str(pathlib.Path(__file__).parent / "memoryctl.py"), subcmd] + args
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}, time.perf_counter() - t0
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {"_error": proc.stderr.strip() or "nonzero exit"}, elapsed
    try:
        return json.loads(proc.stdout), elapsed
    except json.JSONDecodeError:
        return {"_error": "non-json output", "_raw": proc.stdout[:400]}, elapsed


# --- per-query runs ----------------------------------------------------

def run_semantic_search(query: str, provider: str, model: str, limit: int = 5) -> Tuple[Any, float]:
    return run_memoryctl("semantic-search", [
        "--query", query, "--limit", str(limit),
        "--provider", provider, "--model", model,
    ])


def run_hybrid_pack(query: str, provider: str, model: str, limit: int = 5, budget: int = 1800,
                    threshold: float = 0.4) -> Tuple[Any, float]:
    return run_memoryctl("hybrid-pack", [
        "--query", query, "--limit", str(limit),
        "--budget", str(budget), "--threshold", str(threshold),
        "--provider", provider, "--model", model,
    ])


# --- metrics -----------------------------------------------------------

def top_ids(result: Any, k: int = 3) -> List[int]:
    """Extract top-k chapter IDs from a memoryctl result (hybrid-pack/semantic-search)."""
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        return []
    ids = []
    for it in items[:k]:
        if isinstance(it, dict) and "id" in it:
            try:
                ids.append(int(it["id"]))
            except (TypeError, ValueError):
                continue
    return ids


def is_null_retrieval(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("null_retrieval"):
            return True
        items = result.get("items")
        if isinstance(items, list) and len(items) == 0:
            return True
    return False


def precision_at_k(retrieved: List[int], expected: List[int]) -> float:
    if not retrieved or not expected:
        return 0.0
    hits = sum(1 for x in retrieved if x in expected)
    return hits / len(retrieved)


def recall_at_k(retrieved: List[int], expected: List[int]) -> float:
    if not expected:
        return 0.0
    hits = sum(1 for x in expected if x in retrieved)
    return hits / len(expected)


def hit_at(retrieved: List[int], expected: List[int], k: int) -> float:
    if not expected:
        return 0.0
    return 1.0 if any(x in expected for x in retrieved[:k]) else 0.0


# --- report builder ----------------------------------------------------

def build_markdown_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Embedding benchmark — {report['provider']} / {report['model']}")
    lines.append("")
    lines.append(f"- Timestamp: {report['timestamp']}")
    lines.append(f"- Queries: {len(report['results'])}")
    lines.append(f"- Model: `{report['model']}`")
    lines.append("")

    # Storage
    lines.append("## Storage (live DB)")
    lines.append("| provider | model | dims | n | total JSON | avg row | binary/row | binary total |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in report.get("storage", []):
        lines.append(
            f"| {s['provider']} | `{s['model']}` | {s['dims']} | {s['count']} | "
            f"{s.get('total_json_bytes') or '?'} | {s['avg_row_bytes']} | "
            f"{s['binary_bytes_per_row']} | {s['binary_total_bytes']} |"
        )
    lines.append("")

    # Aggregate latency
    sem_lat = [r["semantic"]["latency_s"] for r in report["results"] if r["semantic"].get("latency_s") is not None]
    hyb_lat = [r["hybrid"]["latency_s"] for r in report["results"] if r["hybrid"].get("latency_s") is not None]
    lines.append("## Latency (seconds)")
    lines.append("| mode | p50 | p95 | mean |")
    lines.append("|---|---|---|---|")
    for label, lat in (("semantic-search", sem_lat), ("hybrid-pack", hyb_lat)):
        if lat:
            p50 = statistics.median(lat)
            p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if len(lat) > 1 else lat[0]
            mean = statistics.mean(lat)
            lines.append(f"| {label} | {p50:.3f} | {p95:.3f} | {mean:.3f} |")
    lines.append("")

    # Null retrieval summary
    lines.append("## Null-retrieval rate")
    null_sem = sum(1 for r in report["results"] if r["semantic"].get("null"))
    null_hyb = sum(1 for r in report["results"] if r["hybrid"].get("null"))
    total = len(report["results"])
    lines.append(f"- semantic-search: {null_sem}/{total}")
    lines.append(f"- hybrid-pack: {null_hyb}/{total}")
    lines.append("")

    # Expected-based (only if any query has annotation)
    annotated = [r for r in report["results"] if r["query_meta"].get("has_expected_annotation") and r["query_meta"].get("expected")]
    if annotated:
        lines.append("## Precision / Recall / Hit (expected-annotated queries only)")
        for label in ("semantic", "hybrid"):
            p3 = statistics.mean([r[label].get("precision_3", 0) for r in annotated])
            r3 = statistics.mean([r[label].get("recall_3", 0) for r in annotated])
            h1 = statistics.mean([r[label].get("hit_1", 0) for r in annotated])
            h3 = statistics.mean([r[label].get("hit_3", 0) for r in annotated])
            lines.append(f"- **{label}-search**: P@3={p3:.3f} R@3={r3:.3f} H@1={h1:.3f} H@3={h3:.3f}")
        lines.append("")

    # Per-query detail
    lines.append("## Per-query results")
    for r in report["results"]:
        q = r["query_meta"]
        lines.append(f"### `{q['query']}`")
        if q.get("expected"):
            lines.append(f"- expected: {q['expected']}")
        if q.get("has_expected_annotation") and not q.get("expected"):
            lines.append(f"- (negative control — null expected)")
        for label in ("semantic", "hybrid"):
            sec = r[label]
            if sec.get("error"):
                lines.append(f"- **{label}**: ERROR {sec['error']}")
                continue
            lat = sec.get("latency_s")
            top = sec.get("top_ids", [])
            nullf = sec.get("null")
            extra = ""
            if q.get("expected"):
                extra = f" P@3={sec.get('precision_3', 0):.2f} R@3={sec.get('recall_3', 0):.2f} H@1={sec.get('hit_1', 0):.0f} H@3={sec.get('hit_3', 0):.0f}"
            lines.append(f"- **{label}**: top3={top} lat={lat:.3f}s null={nullf}{extra}")
        lines.append("")

    return "\n".join(lines)


# --- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Embedding benchmark harness")
    ap.add_argument("--queries", required=True, help="path to queries file")
    ap.add_argument("--provider", required=True, choices=["nvidia", "google", "local"])
    ap.add_argument("--model", required=True, help="model identifier for the provider")
    ap.add_argument("--limit", type=int, default=5, help="top-k per search (default 5)")
    ap.add_argument("--output-dir", default=None, help="override output dir")
    args = ap.parse_args()

    info = preflight(args.provider)

    queries = parse_queries(pathlib.Path(args.queries).expanduser())

    out_dir = args.output_dir
    if not out_dir:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        home = os.environ.get("HOME", str(pathlib.Path.home()))
        out_dir = f"{home}/hmk-benchmarks/{ts}-{args.provider}"
    out_path = pathlib.Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    print(f"running {len(queries)} queries; output dir: {out_path}", file=sys.stderr)

    results: List[Dict[str, Any]] = []
    for i, q in enumerate(queries, 1):
        print(f"  [{i}/{len(queries)}] {q['query'][:60]}…", file=sys.stderr)
        sem_res, sem_lat = run_semantic_search(q["query"], args.provider, args.model, args.limit)
        hyb_res, hyb_lat = run_hybrid_pack(q["query"], args.provider, args.model, args.limit)

        sem_top = top_ids(sem_res, k=args.limit)
        hyb_top = top_ids(hyb_res, k=args.limit)

        row: Dict[str, Any] = {
            "query_meta": q,
            "semantic": {
                "top_ids": sem_top,
                "latency_s": sem_lat,
                "null": is_null_retrieval(sem_res),
                "error": sem_res.get("_error") if isinstance(sem_res, dict) else None,
            },
            "hybrid": {
                "top_ids": hyb_top,
                "latency_s": hyb_lat,
                "null": is_null_retrieval(hyb_res),
                "error": hyb_res.get("_error") if isinstance(hyb_res, dict) else None,
            },
        }
        if q["expected"]:
            for label, top in (("semantic", sem_top), ("hybrid", hyb_top)):
                row[label]["precision_3"] = precision_at_k(top[:3], q["expected"])
                row[label]["recall_3"] = recall_at_k(top[:3], q["expected"])
                row[label]["hit_1"] = hit_at(top, q["expected"], 1)
                row[label]["hit_3"] = hit_at(top, q["expected"], 3)
        results.append(row)

    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": args.model,
        "preflight_info": info,
        "results": results,
        "storage": storage_info(info["db_path"]),
    }

    (out_path / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_path / "report.md").write_text(build_markdown_report(report), encoding="utf-8")
    print(f"done: {out_path}/report.{{json,md}}", file=sys.stderr)


if __name__ == "__main__":
    main()
