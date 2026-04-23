#!/usr/bin/env python3
"""Compare two embedding benchmark reports and emit a side-by-side markdown.

Usage (via hmk wrapper):
    ./scripts/hmk embed_benchmark_compare.py \\
        --a $HOME/hmk-benchmarks/20260423-001122-nvidia \\
        --b $HOME/hmk-benchmarks/20260423-001133-google \\
        --output $HOME/hmk-benchmarks/comparison-<ts>.md

Three levels of comparison:
  1. Auto (weak): latency, null-rate, score distribution — with disclaimer.
  2. Expected-based: precision/recall/hit — only if queries were annotated.
  3. Manual review: side-by-side top-5 per query + checklist.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from datetime import datetime
from typing import Any, Dict, List


def load_report(dir_path: pathlib.Path) -> Dict[str, Any]:
    rpath = dir_path / "report.json"
    if not rpath.exists():
        sys.exit(f"ERROR: {rpath} not found")
    return json.loads(rpath.read_text(encoding="utf-8"))


def p50_p95_mean(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0}
    sv = sorted(values)
    return {
        "p50": statistics.median(sv),
        "p95": sv[max(0, int(len(sv) * 0.95) - 1)] if len(sv) > 1 else sv[0],
        "mean": statistics.mean(sv),
    }


def summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    results = report["results"]
    sem_lat = [r["semantic"]["latency_s"] for r in results if r["semantic"].get("latency_s") is not None]
    hyb_lat = [r["hybrid"]["latency_s"] for r in results if r["hybrid"].get("latency_s") is not None]
    annotated = [r for r in results if r["query_meta"].get("expected")]
    summ = {
        "provider": report["provider"],
        "model": report["model"],
        "sem_latency": p50_p95_mean(sem_lat),
        "hyb_latency": p50_p95_mean(hyb_lat),
        "sem_null_rate": sum(1 for r in results if r["semantic"].get("null")) / max(len(results), 1),
        "hyb_null_rate": sum(1 for r in results if r["hybrid"].get("null")) / max(len(results), 1),
        "annotated_count": len(annotated),
        "total_queries": len(results),
    }
    if annotated:
        for label in ("semantic", "hybrid"):
            summ[f"{label}_precision_3"] = statistics.mean([r[label].get("precision_3", 0) for r in annotated])
            summ[f"{label}_recall_3"] = statistics.mean([r[label].get("recall_3", 0) for r in annotated])
            summ[f"{label}_hit_1"] = statistics.mean([r[label].get("hit_1", 0) for r in annotated])
            summ[f"{label}_hit_3"] = statistics.mean([r[label].get("hit_3", 0) for r in annotated])
    return summ


def verdict(sa: Dict[str, Any], sb: Dict[str, Any], margin: float = 0.1, manual_threshold: float = 0.6) -> str:
    """Return a verdict suggestion. Prefer semantic-search quality over hybrid-pack."""
    if sa.get("annotated_count", 0) > 0 and sb.get("annotated_count", 0) > 0:
        a_r = sa.get("semantic_recall_3", 0)
        b_r = sb.get("semantic_recall_3", 0)
        if b_r - a_r >= margin:
            return f"**Google wins** on expected-based semantic recall@3 ({b_r:.2f} vs {a_r:.2f})"
        if a_r - b_r >= margin:
            return f"**NVIDIA wins** on expected-based semantic recall@3 ({a_r:.2f} vs {b_r:.2f})"
        return f"**TIE** — semantic recall@3 diff {abs(a_r - b_r):.2f} < margin {margin:.2f}"
    return "**No verdict (expected annotations missing)** — rely on manual review checklist below"


def build_comparison(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    sa = summarize(a)
    sb = summarize(b)
    lines: List[str] = []
    lines.append(f"# Embedding benchmark — side-by-side")
    lines.append("")
    lines.append(f"- Timestamp: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- A: **{sa['provider']} / {sa['model']}** ({a['timestamp']})")
    lines.append(f"- B: **{sb['provider']} / {sb['model']}** ({b['timestamp']})")
    lines.append(f"- Queries: {sa['total_queries']} (annotated: {sa['annotated_count']})")
    lines.append("")

    # Verdict
    lines.append("## Verdict (auto-suggested)")
    lines.append(verdict(sa, sb))
    lines.append("")
    lines.append("*NB: manual review checklist below is the ground truth if expected annotations are missing or sparse.*")
    lines.append("")

    # Latency
    lines.append("## Latency (seconds)")
    lines.append("| mode | A p50 | A p95 | B p50 | B p95 |")
    lines.append("|---|---|---|---|---|")
    for label, ak, bk in (("semantic", "sem_latency", "sem_latency"), ("hybrid", "hyb_latency", "hyb_latency")):
        al = sa[ak]; bl = sb[bk]
        lines.append(f"| {label} | {al['p50']:.3f} | {al['p95']:.3f} | {bl['p50']:.3f} | {bl['p95']:.3f} |")
    lines.append("")

    # Null rates
    lines.append("## Null-retrieval rate")
    lines.append(f"- A semantic: {sa['sem_null_rate']:.2%} | hybrid: {sa['hyb_null_rate']:.2%}")
    lines.append(f"- B semantic: {sb['sem_null_rate']:.2%} | hybrid: {sb['hyb_null_rate']:.2%}")
    lines.append("")

    # Expected-based
    if sa.get("annotated_count", 0) > 0 and sb.get("annotated_count", 0) > 0:
        lines.append("## Expected-based (Precision / Recall / Hit)")
        lines.append("| metric | A semantic | B semantic | A hybrid | B hybrid |")
        lines.append("|---|---|---|---|---|")
        for m in ("precision_3", "recall_3", "hit_1", "hit_3"):
            lines.append(
                f"| {m} | {sa.get('semantic_'+m, 0):.3f} | {sb.get('semantic_'+m, 0):.3f} | "
                f"{sa.get('hybrid_'+m, 0):.3f} | {sb.get('hybrid_'+m, 0):.3f} |"
            )
        lines.append("")

    # Storage
    if a.get("storage") and b.get("storage"):
        lines.append("## Storage (live DB snapshot)")
        lines.append("| provider | model | dims | n | total JSON | binary total |")
        lines.append("|---|---|---|---|---|---|")
        seen = set()
        for report in (a, b):
            for s in report.get("storage", []):
                key = (s["provider"], s["model"], s["dims"])
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"| {s['provider']} | `{s['model']}` | {s['dims']} | {s['count']} | "
                    f"{s.get('total_json_bytes') or '?'} | {s.get('binary_total_bytes') or '?'} |"
                )
        lines.append("")

    # Per-query side-by-side with manual review checklist
    lines.append("## Per-query side-by-side + manual review")
    lines.append("")
    a_results = {r["query_meta"]["query"]: r for r in a["results"]}
    b_results = {r["query_meta"]["query"]: r for r in b["results"]}
    for q_text in a_results.keys():
        ar = a_results.get(q_text)
        br = b_results.get(q_text)
        lines.append(f"### `{q_text}`")
        if ar and ar["query_meta"].get("expected"):
            lines.append(f"- expected: {ar['query_meta']['expected']}")
        if ar:
            lines.append(f"- **A semantic top5**: {ar['semantic'].get('top_ids')}  (lat={ar['semantic'].get('latency_s'):.3f}s)")
            lines.append(f"- **A hybrid top5**:   {ar['hybrid'].get('top_ids')}  (lat={ar['hybrid'].get('latency_s'):.3f}s)")
        if br:
            lines.append(f"- **B semantic top5**: {br['semantic'].get('top_ids')}  (lat={br['semantic'].get('latency_s'):.3f}s)")
            lines.append(f"- **B hybrid top5**:   {br['hybrid'].get('top_ids')}  (lat={br['hybrid'].get('latency_s'):.3f}s)")
        lines.append("")
        lines.append("Manual verdict:")
        lines.append("- [ ] A wins   - [ ] B wins   - [ ] Tie")
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Compare two embed benchmark reports")
    ap.add_argument("--a", required=True, help="dir of report A (baseline)")
    ap.add_argument("--b", required=True, help="dir of report B (challenger)")
    ap.add_argument("--output", default=None, help="output .md path")
    args = ap.parse_args()

    a = load_report(pathlib.Path(args.a).expanduser())
    b = load_report(pathlib.Path(args.b).expanduser())
    md = build_comparison(a, b)

    if args.output:
        out = pathlib.Path(args.output).expanduser()
    else:
        import os as _os
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        home = _os.environ.get("HOME", str(pathlib.Path.home()))
        out = pathlib.Path(f"{home}/hmk-benchmarks/comparison-{ts}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"comparison written: {out}")


if __name__ == "__main__":
    main()
