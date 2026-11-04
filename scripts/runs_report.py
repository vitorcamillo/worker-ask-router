#!/usr/bin/env python3
"""Resume execucoes do roteador a partir de runs.jsonl (so metricas, sem conteudo)."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _state_dir():
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "worker-ask"
    return Path.home() / ".local" / "state" / "worker-ask"


def _parse_ts(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _load(paths):
    records = []
    invalid = 0
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if not isinstance(item, dict):
                invalid += 1
                continue
            records.append(item)
    return records, invalid


def summarize(records, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for rec in records:
        moment = _parse_ts(rec.get("ts"))
        if moment is not None and moment < cutoff:
            continue
        kept.append(rec)
    total = len(kept)
    rc_counts = {}
    for rec in kept:
        key = str(rec.get("rc"))
        rc_counts[key] = rc_counts.get(key, 0) + 1
    by_executor = {}
    for rec in kept:
        name = rec.get("executor") or "none"
        slot = by_executor.setdefault(name, {"n": 0, "durations": [],
                                             "input": 0, "output": 0,
                                             "cacheRead": 0, "cacheWrite": 0,
                                             "cost_usd": 0.0, "runs_with_usage": 0})
        slot["n"] += 1
        dur = _num(rec.get("duration_s"))
        if dur is not None:
            slot["durations"].append(dur)
        usage = rec.get("usage")
        if isinstance(usage, dict) and usage:
            slot["runs_with_usage"] += int(any(_num(usage.get(f)) is not None for f in ("input", "output")))
            for field in ("input", "output", "cacheRead", "cacheWrite"):
                number = _num(usage.get(field))
                if number is not None:
                    slot[field] += number
            cost = _num(usage.get("cost_usd"))
            if cost is not None:
                slot["cost_usd"] += cost
    executors = {}
    for name, slot in sorted(by_executor.items()):
        durations = slot.pop("durations")
        executors[name] = {**slot,
                           "median_duration_s": statistics.median(durations) if durations else None}
    fallbacks = 0
    for rec in kept:
        attempts = rec.get("attempts")
        if isinstance(attempts, list):
            fallbacks += sum(1 for a in attempts
                             if isinstance(a, dict) and a.get("event") == "fallback")
    spec_groups = {}
    for rec in kept:
        spec = rec.get("spec_sha256")
        cwd = rec.get("cwd")
        if spec and cwd:
            spec_groups.setdefault((spec, cwd), []).append(rec)
    rework_spec_groups = sum(1 for items in spec_groups.values() if len(items) > 1)
    rework_spec_extra = sum(len(items) - 1 for items in spec_groups.values() if len(items) > 1)
    write_groups = {}
    for rec in kept:
        if rec.get("mode") != "write":
            continue
        cwd = rec.get("cwd")
        allowed = rec.get("allowed_files")
        moment = _parse_ts(rec.get("ts"))
        if cwd is None or not isinstance(allowed, list) or moment is None:
            continue
        write_groups.setdefault((cwd, tuple(allowed)), []).append(moment)
    rework_24_groups = 0
    rework_24_extra = 0
    day = timedelta(hours=24)
    for moments in write_groups.values():
        ordered = sorted(moments)
        extra = sum(1 for i in range(1, len(ordered)) if ordered[i] - ordered[i - 1] <= day)
        if extra > 0:
            rework_24_groups += 1
            rework_24_extra += extra
    no_change = sum(1 for rec in kept
                    if rec.get("mode") == "write" and rec.get("rc") == 0
                    and rec.get("changed_files") == 0)
    return {
        "total": total,
        "days": days,
        "rc_counts": rc_counts,
        "by_executor": executors,
        "fallbacks_total": fallbacks,
        "fallbacks_per_run": (fallbacks / total) if total else 0.0,
        "rework_same_spec": {"groups": rework_spec_groups, "extra_runs": rework_spec_extra},
        "rework_write_24h": {"groups": rework_24_groups, "extra_runs": rework_24_extra},
        "write_no_change": no_change,
    }


def _print_text(report, invalid):
    print(f"execucoes: {report['total']} (ultimos {report['days']} dias; {invalid} linhas invalidas ignoradas)")
    print("rc:", ", ".join(f"{k}={v}" for k, v in sorted(report["rc_counts"].items())) or "nenhum")
    for name, slot in report["by_executor"].items():
        print(f"executor {name}: n={slot['n']} mediana_s={slot['median_duration_s']} "
              f"input={slot['input']} output={slot['output']} "
              f"cacheRead={slot['cacheRead']} cacheWrite={slot['cacheWrite']} "
              f"cost_usd={slot['cost_usd']} runs_with_usage={slot['runs_with_usage']}")
    print(f"fallbacks: total={report['fallbacks_total']} media_por_execucao={report['fallbacks_per_run']:.2f}")
    spec = report["rework_same_spec"]
    print(f"retrabalho mesmo spec/cwd: grupos={spec['groups']} execucoes_extras={spec['extra_runs']}")
    day = report["rework_write_24h"]
    print(f"retrabalho write 24h: grupos={day['groups']} execucoes_extras={day['extra_runs']}")
    print(f"write rc0 sem mudanca (changed_files=0): {report['write_no_change']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="runs_report.py")
    parser.add_argument("--file", default=str(_state_dir() / "runs.jsonl"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.days is None or args.days < 0:
        parser.error("--days deve ser inteiro >= 0")
    main_path = Path(args.file)
    paths = [main_path, main_path.with_name(main_path.name + ".1")]
    records, invalid = _load([p for p in paths if p.exists() or p == main_path])
    report = summarize(records, args.days)
    report["invalid_lines"] = invalid
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_text(report, invalid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
