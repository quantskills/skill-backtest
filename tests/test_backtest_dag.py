import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dag", ROOT / "scripts" / "backtest_dag.py")
dag = importlib.util.module_from_spec(spec); spec.loader.exec_module(dag)

class DagTest(unittest.TestCase):
    def setUp(self):
        f = ROOT / "tests" / "fixtures"; self.factor = json.loads((f / "factor-panel.json").read_text()); self.market = json.loads((f / "market-bar-trading.json").read_text())
    def test_valid_outputs_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp); backtest, evaluation = dag.run(self.factor, self.market, out, "fixture", 1, .5, 15, "fixture-alpha")
            self.assertEqual(backtest["$contract"]["profile"], "backtest-result"); self.assertEqual(evaluation["$contract"]["profile"], "evaluation-result")
            self.assertTrue((out / "return-series.json").is_file()); self.assertNotEqual(evaluation["payload"]["records"][0]["lineage"]["sources"][0]["profile"], "evaluation-result")
            self.assertEqual(evaluation["payload"]["records"][0]["lineage"]["sources"][0]["sha256"], "sha256:" + hashlib.sha256((out / "backtest-result.json").read_bytes()).hexdigest())
            self.assertEqual(backtest["payload"]["records"][0]["assumptions"]["return_series_sha256"], "sha256:" + hashlib.sha256((out / "return-series.json").read_bytes()).hexdigest())
    def test_open_execution_and_untradable_exit_delay(self):
        market = copy.deepcopy(self.market)
        for row in market["payload"]["records"] + market["payload"]["native"]["raw_records"]:
            if row["instrument_id"] == "AAA" and row["timestamp"].startswith("2026-08-02"):
                row.update(open=20, close=21, limit_up=30, limit_down=1)
        extra = {"instrument_id":"AAA","timestamp":"2026-08-03T09:30:00Z","open":30,"high":31,"low":29,"close":30,"volume":100,"frequency":"1d","adjustment":"none","calendar":"SYNX","trade_status":1,"limit_up":40,"limit_down":20,"limit_basis":"close"}
        market["payload"]["records"].append(copy.deepcopy(extra)); market["payload"]["native"]["raw_records"].append(copy.deepcopy(extra))
        extra["timestamp"] = "2026-08-04T09:30:00Z"; extra.update(open=40, high=41, low=39, close=40, trade_status=0, limit_down=30)
        market["payload"]["records"].append(copy.deepcopy(extra)); market["payload"]["native"]["raw_records"].append(copy.deepcopy(extra))
        with tempfile.TemporaryDirectory() as temp:
            _, evaluation = dag.run(self.factor, market, Path(temp), "fixture", 1, .5, 0, "fixture-alpha")
            events = json.loads((Path(temp) / "return-series.json").read_text())
            self.assertEqual(events[1]["entry_price"], 20)
            self.assertEqual(events[2]["exit_count"], 0)
            self.assertEqual(events[3]["exit_price"], 40)
    def test_cli_lineage_hashes_input_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"; fixture = ROOT / "tests" / "fixtures"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "backtest_dag.py"), "--factor", str(fixture / "factor-panel.json"), "--market", str(fixture / "market-bar-trading.json"), "--output-dir", str(out), "--strategy-id", "fixture", "--horizon", "1", "--top-pct", "0.5", "--fee-bps", "15", "--factor-id", "fixture-alpha"], check=True, capture_output=True, text=True)
            result = json.loads((out / "backtest-result.json").read_text())
            hashes = [x["sha256"] for x in result["payload"]["records"][0]["lineage"]["sources"]]
            self.assertEqual(hashes, ["sha256:" + hashlib.sha256((fixture / "factor-panel.json").read_bytes()).hexdigest(), "sha256:" + hashlib.sha256((fixture / "market-bar-trading.json").read_bytes()).hexdigest()])
    def test_ohlcv_only_fixture_fails(self):
        bad = json.loads((ROOT / "tests" / "fixtures" / "market-bar-ohlcv-only.json").read_text())
        with self.assertRaises(dag.ContractError): dag.validate_market(bad)
    def test_multiple_factor_ids_and_native_mismatch_fail(self):
        factor = copy.deepcopy(self.factor); factor["payload"]["records"][1]["factor_id"] = "other"
        with self.assertRaises(dag.ContractError): dag.validate_factor(factor, None)
        market = copy.deepcopy(self.market); market["payload"]["native"]["raw_records"][0]["close"] = 99
        with self.assertRaises(dag.ContractError): dag.validate_market(market)

if __name__ == "__main__": unittest.main()
