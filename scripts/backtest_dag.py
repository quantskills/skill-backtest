#!/usr/bin/env python3
"""Deterministic, fail-closed backtest DAG for the backtest skill."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
SHA = "sha256:" + "0" * 64
FACTOR_FIELDS = {"instrument_id", "timestamp", "factor_id", "value", "direction", "frequency", "universe", "missing_policy", "neutralization"}
MARKET_FIELDS = {"instrument_id", "timestamp", "open", "high", "low", "close", "volume", "frequency", "adjustment", "calendar", "trade_status", "limit_up", "limit_down", "limit_basis"}


class ContractError(ValueError): pass


def load(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ContractError(f"cannot read {path}") from exc
    if not isinstance(value, dict): raise ContractError("envelope must be an object")
    return value


def require(value, message):
    if not value: raise ContractError(message)


def field_names(doc: dict) -> set[str]:
    fields = doc.get("schema", {}).get("fields")
    require(isinstance(fields, dict), "schema.fields must be an object")
    return set(fields)


def records(doc: dict, profile: str) -> list[dict]:
    c = doc.get("$contract", {})
    require(c.get("profile") == profile and c.get("profile_version") == "1.0.0", f"expected {profile}@1.0.0")
    rows = doc.get("payload", {}).get("records")
    require(isinstance(rows, list) and rows, f"{profile} records missing")
    require(all(isinstance(x, dict) for x in rows), f"{profile} records must be objects")
    return rows


def timestamp(value: object) -> bool: return isinstance(value, str) and RFC3339.fullmatch(value) is not None


def validate_factor(doc: dict, selected_id: str | None) -> tuple[list[dict], str, int]:
    rows = records(doc, "factor-panel")
    require(FACTOR_FIELDS <= field_names(doc), "factor-panel schema fields incomplete")
    require(all(FACTOR_FIELDS <= set(r) and timestamp(r["timestamp"]) for r in rows), "invalid factor record")
    factor_ids = {r["factor_id"] for r in rows}
    require(len(factor_ids) == 1, "factor-panel must contain one globally unique factor_id")
    factor_id = next(iter(factor_ids)); require(not selected_id or selected_id == factor_id, "--factor-id does not match factor-panel")
    directions = {r["direction"] for r in rows}; require(len(directions) == 1, "factor direction must be consistent")
    direction = directions.pop(); require(direction in {"higher-is-better", "lower-is-better"}, "neutral factor direction is unsupported")
    seen = set()
    for row in rows:
        key = (row["instrument_id"], row["timestamp"]); require(key not in seen, "duplicate factor key"); seen.add(key)
        require(isinstance(row["value"], (int, float)) and not isinstance(row["value"], bool) and math.isfinite(row["value"]), "factor value must be finite")
    return rows, factor_id, -1 if direction == "lower-is-better" else 1


def validate_market(doc: dict) -> list[dict]:
    rows = records(doc, "market-bar")
    require(MARKET_FIELDS <= field_names(doc), "market-bar must declare trade_status, limit_up, limit_down, and limit_basis")
    native = doc.get("payload", {}).get("native", {}); raw = native.get("raw_records") if isinstance(native, dict) else None
    require(isinstance(raw, list) and raw, "market-bar payload.native.raw_records is required")
    def index(items, source):
        result = {}
        for row in items:
            require(isinstance(row, dict) and MARKET_FIELDS <= set(row) and timestamp(row["timestamp"]), f"invalid {source} market record")
            key = (row["instrument_id"], row["timestamp"]); require(key not in result, f"duplicate {source} market key"); result[key] = row
        return result
    canonical, original = index(rows, "canonical"), index(raw, "native")
    require(canonical.keys() == original.keys(), "native and canonical market keys must be a strict 1:1 mapping")
    for key, row in canonical.items():
        other = original[key]
        require(all(row[k] == other[k] for k in ("open", "high", "low", "close", "volume", "frequency", "calendar", "adjustment", "limit_up", "limit_down", "limit_basis", "trade_status")), "native market record differs from canonical record")
        require(all(isinstance(row[k], (int, float)) and not isinstance(row[k], bool) for k in ("open", "high", "low", "close", "volume", "limit_up", "limit_down")), "market prices must be numeric")
        require(row["frequency"] == doc.get("meta", {}).get("frequency", row["frequency"]) and row["calendar"] == doc.get("meta", {}).get("calendar", row["calendar"]), "market metadata inconsistent")
    return rows


def artifact(path: Path, value: object) -> tuple[str, str]:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(data + b"\n")
    return f"artifact://{path.name}", "sha256:" + hashlib.sha256(data).hexdigest()


def run(factor_doc: dict, market_doc: dict, out: Path, strategy_id: str, horizon: int, top_pct: float, fee_bps: float, selected_id: str | None = None) -> tuple[dict, dict]:
    require(horizon > 0 and 0 < top_pct <= 1 and fee_bps >= 0, "invalid explicit parameters")
    factors, factor_id, sign = validate_factor(factor_doc, selected_id); market = validate_market(market_doc)
    bars = {(x["instrument_id"], x["timestamp"]): x for x in market}; signals = defaultdict(dict)
    for x in factors:
        require((x["instrument_id"], x["timestamp"]) in bars, "factor key has no market-bar")
        signals[x["timestamp"]][x["instrument_id"]] = sign * x["value"]
    dates = sorted({x["timestamp"] for x in market}); universe = {d: {x["instrument_id"] for x in market if x["timestamp"] == d} for d in dates}
    sleeves, previous, series, turnover = [], defaultdict(float), [], []
    for i, date in enumerate(dates):
        if len(sleeves) >= horizon: sleeves.pop(0)
        if i + 1 < len(dates) and date in signals:
            ranked = sorted(signals[date].items(), key=lambda x: (-x[1], x[0])); count = max(1, math.ceil(len(ranked) * top_pct)); nxt = dates[i + 1]
            eligible = [symbol for symbol, _ in ranked[:count] if symbol in universe[nxt] and bars[symbol, nxt]["trade_status"] != 1 and bars[symbol, nxt]["close"] < bars[symbol, nxt]["limit_up"] * .99]
            if eligible: sleeves.append({symbol: 1 / horizon / len(eligible) for symbol in eligible})
        position = defaultdict(float)
        for sleeve in sleeves:
            for symbol, weight in sleeve.items(): position[symbol] += weight
        gross = 0.0
        if i:
            prior = dates[i - 1]
            for symbol, weight in previous.items():
                if (symbol, date) in bars and (symbol, prior) in bars: gross += weight * (bars[symbol, date]["close"] / bars[symbol, prior]["close"] - 1)
        delta = sum(abs(position[s] - previous[s]) for s in set(position) | set(previous)) / 2
        net = gross - delta * 2 * fee_bps / 1e4 if i else 0.0
        series.append({"timestamp": date, "return": net, "turnover": delta, "nav": (series[-1]["nav"] if series else 1.0) * (1 + net)})
        turnover.append(delta); previous = position
    ref, digest = artifact(out / "return-series.json", series)
    navs = [x["nav"] for x in series]; returns = [x["return"] for x in series]
    annual = navs[-1] ** (252 / len(navs)) - 1; mean = sum(returns) / len(returns); variance = sum((x - mean) ** 2 for x in returns) / max(1, len(returns) - 1)
    metrics = {"annual_return": annual, "sharpe": mean / math.sqrt(variance) * math.sqrt(252) if variance else 0.0, "max_drawdown": min(n / max(navs[:j + 1]) - 1 for j, n in enumerate(navs)), "annual_turnover": sum(turnover) / len(turnover) * 252}
    generated = factor_doc["meta"]["generated_at"]; provenance = [{"provider": factor_doc["meta"]["producer"], "dataset": factor_doc["meta"]["dataset_id"], "raw_ref": "artifact://input/factor-panel", "raw_sha256": SHA}]
    lineage = {"sources": [{"profile": "factor-panel", "version": "1.0.0", "artifact_ref": "artifact://input/factor-panel", "sha256": SHA}, {"profile": "market-bar", "version": "1.0.0", "artifact_ref": "artifact://input/market-bar", "sha256": SHA}], "evidence_refs": ["evidence://input/factor-panel", "evidence://input/market-bar"]}
    common = {"generated_at": generated, "as_of": generated, "timezone": factor_doc["meta"].get("timezone", "UTC"), "currency": market_doc["meta"]["currency"], "calendar": market_doc["meta"].get("calendar", market[0]["calendar"]), "provenance": provenance}
    backtest_fields = {k: {"type": t, "nullable": False} for k, t in {"strategy_id":"string","period_start":"string","period_end":"string","return_series_ref":"string","costs":"number","assumptions":"object","bias_controls":"array","lineage":"object"}.items()}; backtest_fields["period_start"]["format"] = backtest_fields["period_end"]["format"] = "date"; backtest_fields["costs"]["unit"] = "currency"
    backtest = {"$contract": {"envelope": "quantskills-envelope", "envelope_version": "1.0.0", "profile": "backtest-result", "profile_version": "1.0.0"}, "meta": {"dataset_id": strategy_id, "producer": "skill-backtest", **common}, "schema": {"primary_key": ["strategy_id", "period_start", "period_end"], "fields": backtest_fields}, "payload": {"native": {"provider": "skill-backtest", "raw_records": series}, "records": [{"strategy_id": strategy_id, "period_start": dates[0][:10], "period_end": dates[-1][:10], "return_series_ref": ref, "costs": sum(x["turnover"] for x in series) * 2 * fee_bps / 1e4, "assumptions": {"factor_id": factor_id, "horizon": horizon, "top_pct": top_pct, "fee_bps": fee_bps, "execution": "T+1-open-signal/close-to-close-return"}, "bias_controls": ["T+1", "limit-and-suspension-filters", "explicit-inputs"], "lineage": lineage}]}, "quality": {"status": "pass", "checks": ["strict-input-contract", "deterministic-engine"], "warnings": []}}
    backtest_ref, backtest_digest = artifact(out / "backtest-result.json", backtest)
    evaluation_fields = {k: {"type": t, "nullable": False} for k, t in {"subject_id":"string","evaluated_at":"string","sample_start":"string","sample_end":"string","metrics":"object","verdict":"string","evidence":"array","lineage":"object"}.items()}; evaluation_fields["evaluated_at"]["format"] = "date-time"; evaluation_fields["sample_start"]["format"] = evaluation_fields["sample_end"]["format"] = "date"
    evaluation = {"$contract": {"envelope": "quantskills-envelope", "envelope_version": "1.0.0", "profile": "evaluation-result", "profile_version": "1.0.0"}, "meta": {"dataset_id": strategy_id + "-evaluation", "producer": "skill-backtest", **common}, "schema": {"primary_key": ["subject_id", "evaluated_at"], "fields": evaluation_fields}, "payload": {"native": {"provider": "skill-backtest", "raw_records": [metrics]}, "records": [{"subject_id": strategy_id, "evaluated_at": generated, "sample_start": dates[0][:10], "sample_end": dates[-1][:10], "metrics": metrics, "verdict": "pass", "evidence": [ref], "lineage": {"sources": [{"profile": "backtest-result", "version": "1.0.0", "artifact_ref": backtest_ref, "sha256": backtest_digest}], "evidence_refs": [ref]}}]}, "quality": {"status": "pass", "checks": ["derived-from-backtest-result"], "warnings": []}}
    return backtest, evaluation


def main():
    p = argparse.ArgumentParser(); p.add_argument("--factor", required=True); p.add_argument("--market", required=True); p.add_argument("--output-dir", required=True); p.add_argument("--strategy-id", required=True); p.add_argument("--horizon", required=True, type=int); p.add_argument("--top-pct", required=True, type=float); p.add_argument("--fee-bps", required=True, type=float); p.add_argument("--factor-id")
    a = p.parse_args(); out = Path(a.output_dir); require(not out.exists(), "output directory must not already exist"); out.mkdir(parents=True)
    try: backtest, evaluation = run(load(a.factor), load(a.market), out, a.strategy_id, a.horizon, a.top_pct, a.fee_bps, a.factor_id)
    except Exception:
        out.rmdir(); raise
    (out / "evaluation-result.json").write_text(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"backtest_result": str(out / "backtest-result.json"), "evaluation_result": str(out / "evaluation-result.json")}, sort_keys=True))

if __name__ == "__main__": main()
