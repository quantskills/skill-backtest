import copy
import importlib.util
import json
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
    def test_ohlcv_only_fixture_fails(self):
        bad = json.loads((ROOT / "tests" / "fixtures" / "market-bar-ohlcv-only.json").read_text())
        with self.assertRaises(dag.ContractError): dag.validate_market(bad)
    def test_multiple_factor_ids_and_native_mismatch_fail(self):
        factor = copy.deepcopy(self.factor); factor["payload"]["records"][1]["factor_id"] = "other"
        with self.assertRaises(dag.ContractError): dag.validate_factor(factor, None)
        market = copy.deepcopy(self.market); market["payload"]["native"]["raw_records"][0]["close"] = 99
        with self.assertRaises(dag.ContractError): dag.validate_market(market)

if __name__ == "__main__": unittest.main()
