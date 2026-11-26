"""Check both strategy entry points from a working directory outside the checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JointCliRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def cli(self, *args):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "etf_arb.cli", "backtest", *args],
                              cwd=self.work, env=env, capture_output=True, encoding="utf-8")

    def test_default_launcher_forwards_options_and_resolves_input_paths_from_caller(self):
        (self.work / "holdings.xlsx").touch()
        (self.work / "references.csv").touch()
        run = self.cli("--check-only", "--pcf-root", ".", "--minute-root", ".",
                       "--odd-lot-holdings", "holdings.xlsx", "--odd-lot-reference-prices", "references.csv",
                       "--creation-threshold", "0.0018", "--redemption-threshold", "0.0014")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("creation 0.1800%, redemption 0.1400%", run.stdout)

    def test_explicit_joint_config_uses_profile_cli_and_explains_unsupported_override(self):
        cfg = json.loads((ROOT / "configs/159338_joint.json").read_text(encoding="utf-8"))
        (self.work / "joint.json").write_text(json.dumps(cfg), encoding="utf-8")
        run = self.cli("--config", "joint.json", "--stock-suspensions", "overrides.csv")
        self.assertEqual(run.returncode, 2)
        self.assertIn("unrecognized arguments: --stock-suspensions", run.stderr)

    def test_unknown_flag_with_legacy_config_is_rejected(self):
        (self.work / "simple.json").write_text("{}", encoding="utf-8")
        run = self.cli("--config", "simple.json", "--unexpected-flag")
        self.assertEqual(run.returncode, 2)
        self.assertIn("unrecognized arguments: --unexpected-flag", run.stderr)
