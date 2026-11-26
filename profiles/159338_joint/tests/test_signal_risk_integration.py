"""Verify the report-only risk module reaches fresh and reconstructed reports."""

import hashlib
import json
import unittest
from unittest.mock import patch

import test_full_flow as fixtures
from etf_arb.report.rebuild import rebuild_run_report
from etf_arb.report.signal_risk import build_signal_risk, signal_risk_html


class SignalRiskIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def assert_rendered(self, directory, data):
        self.assertEqual(json.loads((directory / 'signal_risk.json').read_text(encoding='utf-8')), data)
        self.assertTrue((directory / 'trade_signal_risk.csv').is_file())
        self.assertTrue((directory / 'stock_signal_risk.csv').is_file())
        self.assertTrue((directory / 'signal_risk.html').is_file())
        report = (directory / 'report.html').read_text(encoding='utf-8')
        self.assertIn(signal_risk_html(data), report)
        ledger = (directory / 'ledger.html').read_text(encoding='utf-8')
        for trade in data['trades']:
            self.assertIn(signal_risk_html(data, trade_id=trade['trade_id']), ledger)

    def test_full_runner_and_cached_or_legacy_rebuild_export_the_same_risk(self):
        with patch('etf_arb.full_flow.build_signal_risk', wraps=build_signal_risk) as build:
            runner, summary = self.fixture.run_model()
        build.assert_called_once()
        self.assertIs(build.call_args.kwargs['rules'], runner.resources.rules)
        run = self.fixture.config.output_dir
        data = json.loads((run / 'signal_risk.json').read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(data['selection_basis'], 'signal_time_directional_risk')
        self.assertEqual(data['component_scope'], 'exclude_cross_market_cash_substitution')
        self.assertEqual(len(data['trades']), summary['opened_count'])
        # The fixture has local and foreign fixed cash; only local risks remain.
        self.assertEqual({row['symbol'] for row in data['rows']}, {'000002.SZ'})
        self.assertTrue(all(row['etf_market'] == 'SZ' for row in data['trades']))
        self.assertTrue(all(row['excluded_cross_market_cash_count'] == 2 for row in data['trades']))
        self.assert_rendered(run / 'report', data)
        original = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in run.rglob('*') if p.is_file()}
        cached_output = self.fixture.root / 'cached-report'
        with patch('etf_arb.report.signal_risk.build_signal_risk',
                   side_effect=AssertionError('cached report must not recalculate signal risk')):
            rebuild_run_report(run, cached_output)
        self.assert_rendered(cached_output, data)
        self.assertEqual(original, {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in run.rglob('*') if p.is_file()})
        # Emulate an older completed run with no risk snapshot, without rerunning trades.
        (run / 'signal_risk.json').unlink()
        old_run = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in run.rglob('*') if p.is_file()}
        legacy_output = self.fixture.root / 'legacy-report'
        with patch('etf_arb.report.signal_risk.build_signal_risk', wraps=build_signal_risk) as rebuild:
            rebuild_run_report(run, legacy_output)
        rebuild.assert_called_once()
        self.assert_rendered(legacy_output, data)
        self.assertEqual(old_run, {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in run.rglob('*') if p.is_file()})

    def test_old_schema_and_wrong_scope_rebuild_without_changing_execution_files(self):
        self.fixture.run_model()
        run = self.fixture.config.output_dir
        cache = run / 'signal_risk.json'
        current = json.loads(cache.read_text(encoding='utf-8'))
        for name, version, scope in (('v1', 1, None), ('wrong-scope', 2, 'all_constituents')):
            with self.subTest(name=name):
                old = json.loads(json.dumps(current))
                old['schema_version'] = version
                if scope is None:
                    old.pop('component_scope')
                else:
                    old['component_scope'] = scope
                # A plausible stale foreign constituent must be recomputed, never relabelled.
                old['rows'][0]['symbol'] = '600001.SH'
                old['rows'][0]['signal_value'] = '999999'
                cache.write_text(json.dumps(old), encoding='utf-8')
                original = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in run.rglob('*') if path.is_file()}
                output = self.fixture.root / f'rebuild-{name}'
                with patch('etf_arb.report.signal_risk.build_signal_risk',
                           wraps=build_signal_risk) as rebuild:
                    rebuild_run_report(run, output)
                rebuild.assert_called_once()
                self.assert_rendered(output, current)
                self.assertEqual(original, {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                           for path in run.rglob('*') if path.is_file()})


if __name__ == '__main__':
    unittest.main()
