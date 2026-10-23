import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import run_integrated_test as harness
from run_integrated_test import FreshGpsFixGate, has_fresh_gps_fix


class FreshGpsFixGateTests(unittest.TestCase):
    def test_requires_valid_non_fallback_coordinates(self):
        base = {
            "fix_valid": True,
            "using_last_known": False,
            "latitude": 9.5,
            "longitude": 6.4,
        }
        self.assertTrue(has_fresh_gps_fix(base))
        self.assertFalse(has_fresh_gps_fix({**base, "fix_valid": False}))
        self.assertFalse(has_fresh_gps_fix({**base, "using_last_known": True}))
        self.assertFalse(has_fresh_gps_fix({**base, "latitude": float("nan")}))
        self.assertFalse(has_fresh_gps_fix({**base, "longitude": 181}))

    def test_requires_three_distinct_consecutive_fresh_samples(self):
        gate = FreshGpsFixGate()
        sample = {
            "fix_valid": True,
            "using_last_known": False,
            "latitude": 9.5,
            "longitude": 6.4,
        }

        self.assertFalse(gate.observe({**sample, "source_updated_at": "08:00:01"}))
        self.assertFalse(gate.observe({**sample, "source_updated_at": "08:00:01"}))
        self.assertFalse(gate.observe({**sample, "source_updated_at": "08:00:02"}))
        self.assertTrue(gate.observe({**sample, "source_updated_at": "08:00:03"}))
        self.assertTrue(gate.ready)

    def test_invalid_sample_resets_consecutive_count(self):
        gate = FreshGpsFixGate()
        sample = {
            "fix_valid": True,
            "using_last_known": False,
            "latitude": 9.5,
            "longitude": 6.4,
        }

        gate.observe({**sample, "source_updated_at": "08:00:01"})
        gate.observe({**sample, "source_updated_at": "08:00:02"})
        self.assertFalse(gate.observe({
            **sample,
            "fix_valid": False,
            "source_updated_at": "08:00:03",
        }))
        self.assertFalse(gate.ready)
        self.assertFalse(gate.observe({**sample, "source_updated_at": "08:00:04"}))
        self.assertFalse(gate.observe({**sample, "source_updated_at": "08:00:05"}))
        self.assertTrue(gate.observe({**sample, "source_updated_at": "08:00:06"}))

    def test_pre_phase_waits_for_fresh_fix_before_completing(self):
        clock = [1000.0]

        class Recorder:
            fetch_failures = 0
            last = {}

            def __init__(self):
                self.sample_number = 0

            def sample(self, phase, session, start_epoch):
                self.sample_number += 1
                self.last = {
                    "fix_valid": True,
                    "using_last_known": False,
                    "latitude": 9.5,
                    "longitude": 6.4,
                    "source_updated_at": str(self.sample_number),
                    "timestamp_utc": str(self.sample_number),
                }
                return self.last

        class Lcd:
            def show(self, *args, **kwargs):
                pass

        harness._stop.update(flag=False, reason=None)
        self.addCleanup(harness._stop.update, flag=False, reason=None)
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder()
            phases = []
            with patch.object(harness.time, "time", side_effect=lambda: clock[0]), \
                    patch.object(harness.time, "sleep",
                                 side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)), \
                    patch.object(harness, "temp_c", return_value=42.0):
                phase = harness.run_phase(
                    recorder, Lcd(), phases, str(pathlib.Path(tmp) / "phases.json"),
                    "pre", 0, 0, 1, SimpleNamespace(max_temp=86.0),
                    gps_wait_sec=5,
                )

        self.assertTrue(phase["gps_fix_ready"])
        self.assertEqual(recorder.sample_number, 3)
        self.assertGreaterEqual(clock[0], 1002.0)
        self.assertFalse(harness._stop["flag"])

    def test_pre_phase_timeout_stops_without_a_gps_fix(self):
        clock = [1000.0]

        class Recorder:
            fetch_failures = 0
            last = {}

            def sample(self, phase, session, start_epoch):
                self.last = {
                    "fix_valid": False,
                    "using_last_known": True,
                    "latitude": 9.5,
                    "longitude": 6.4,
                    "source_updated_at": str(clock[0]),
                    "timestamp_utc": str(clock[0]),
                }
                return self.last

        class Lcd:
            def show(self, *args, **kwargs):
                pass

        harness._stop.update(flag=False, reason=None)
        self.addCleanup(harness._stop.update, flag=False, reason=None)
        with tempfile.TemporaryDirectory() as tmp:
            phases = []
            with patch.object(harness.time, "time", side_effect=lambda: clock[0]), \
                    patch.object(harness.time, "sleep",
                                 side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)), \
                    patch.object(harness, "temp_c", return_value=42.0):
                phase = harness.run_phase(
                    Recorder(), Lcd(), phases, str(pathlib.Path(tmp) / "phases.json"),
                    "pre", 0, 0, 1, SimpleNamespace(max_temp=86.0),
                    gps_wait_sec=2,
                )

        self.assertFalse(phase["gps_fix_ready"])
        self.assertTrue(phase["gps_wait_timed_out"])
        self.assertTrue(harness._stop["flag"])
        self.assertIn("no measurement sessions started", harness._stop["reason"])

    def test_boot_gps_timeout_disarms_and_preserves_run(self):
        state = {
            "armed": True,
            "run_dir": "/tmp/gps-test",
            "plan": {"boot_delay": 0},
        }
        summary = {
            "aborted": "fresh GPS fix not acquired within 900 seconds; "
                       "no measurement sessions started"
        }
        writes = []
        with patch.object(harness, "read_json", side_effect=[state, summary]), \
                patch.object(harness, "run_campaign", return_value=1), \
                patch.object(harness, "write_json",
                             side_effect=lambda path, value: writes.append((path, value))), \
                patch.object(harness.os, "remove") as remove_state, \
                patch("builtins.print"):
            result = harness.boot_campaign()

        self.assertEqual(result, 1)
        self.assertFalse(state["armed"])
        self.assertEqual(state["aborted"], summary["aborted"])
        self.assertEqual(writes[0][0], harness.DONE_PATH)
        remove_state.assert_called_once_with(harness.STATE_PATH)


if __name__ == "__main__":
    unittest.main()
