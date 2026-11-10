from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from rich.console import Console

from npu_top import dashboard, main, parse_info, summary


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


class TelemetryTests(unittest.TestCase):
    def test_multiple_dies_and_physical_ids(self):
        sample = parse_info(fixture("dual_die.txt"))
        self.assertEqual([(d.board, d.chip, d.physical) for d in sample.devices], [(3, 0, 6), (3, 1, 9)])
        self.assertEqual([d.utilization for d in sample.devices], [75.0, 25.0])
        self.assertEqual([(d.memory_used, d.memory_total, d.memory_kind) for d in sample.devices],
                         [(4096.0, 65536.0, "HBM"), (8192.0, 65536.0, "HBM")])
        self.assertEqual([(p.pid, p.chip, p.memory) for p in sample.processes], [(1234, 0, 4096.0), (1234, 1, 8192.0)])

    def test_board_power_is_counted_once(self):
        output = StringIO()
        Console(file=output, width=140, color_system=None).print(summary(parse_info(fixture("dual_die.txt")).devices, False))
        self.assertIn("0.30 kW", output.getvalue())
        self.assertIn("50.0%", output.getvalue())
        self.assertIn("12.0 GiB", output.getvalue())

    def test_single_chip_memory_only(self):
        sample = parse_info(fixture("memory_only.txt"))
        device, = sample.devices
        self.assertEqual((device.board, device.chip, device.physical), (5, 0, 7))
        self.assertEqual((device.memory_used, device.memory_total, device.memory_kind), (1024.0, 24576.0, "MEMORY"))
        output = StringIO()
        Console(file=output, width=140, height=40, color_system=None).print(dashboard(sample, None, {}, 2, 140, 40))
        self.assertIn("MEMORY USED", output.getvalue())
        self.assertIn("MEM GiB", output.getvalue())
        self.assertNotIn("HBM", output.getvalue())

    def test_ddr_header(self):
        sample = parse_info(fixture("memory_only.txt").replace("Memory-Usage(MB)", "DDR-Usage(MB)"))
        self.assertEqual(sample.devices[0].memory_kind, "DDR")
        self.assertEqual(sample.devices[0].memory_total, 24576.0)

    def test_missing_values_are_not_zero(self):
        sample = parse_info(fixture("memory_only.txt").replace("36 1024 / 24576", "N/A N/A / N/A"))
        device = sample.devices[0]
        self.assertIsNone(device.utilization)
        self.assertIsNone(device.memory_used)
        self.assertIsNone(device.memory_total)
        self.assertIsNone(device.power)

    def test_missing_hbm_does_not_become_host_memory(self):
        sample = parse_info(fixture("dual_die.txt").replace("4096 / 65536", "- / -"))
        self.assertIsNone(sample.devices[0].memory_used)
        self.assertIsNone(sample.devices[0].memory_total)
        self.assertEqual(sample.devices[0].memory_kind, "HBM")

    def test_missing_process_table_is_unavailable(self):
        data = fixture("memory_only.txt").split("| NPU   Chip")[0]
        sample = parse_info(data)
        self.assertFalse(sample.processes_available)
        output = StringIO()
        Console(file=output, width=140, height=40, color_system=None).print(dashboard(sample, None, {}, 2, 140, 40))
        self.assertIn("Process information unavailable", output.getvalue())
        self.assertNotIn("No running NPU processes", output.getvalue())

    def test_incomplete_device_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_info(fixture("dual_die.txt").replace("| 1     9", "# 1     9"))

    def test_unsupported_memory_layout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "memory layout"):
            parse_info(fixture("dual_die.txt").replace("HBM-Usage(MB)", "Unrecognized(MB)"))

    def test_no_devices_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_info("Driver unavailable")

    def test_process_control_characters_are_removed(self):
        sample = parse_info(fixture("dual_die.txt").replace("training-worker", "bad\x1b[31m-worker"))
        self.assertNotIn("\x1b", sample.processes[0].name)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.driver = Path(self.directory.name) / "npu-smi"

    def write_driver(self, body):
        self.driver.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        self.driver.chmod(0o755)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "npu_top.py"), "--npu-smi", str(self.driver), *args],
                              capture_output=True, text=True, timeout=20)

    def test_json_from_driver_command(self):
        self.write_driver(f"import sys\nassert sys.argv[1:] == ['info']\nprint({fixture('dual_die.txt')!r})\n")
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        sample = json.loads(result.stdout)
        self.assertEqual([(d["physical"], d["utilization"]) for d in sample["devices"]], [(6, 75.0), (9, 25.0)])
        self.assertEqual(sample["processes"][0]["pid"], 1234)
        self.assertEqual(result.stderr, "")

    def test_doctor_checks_real_command_and_parser(self):
        self.write_driver(f"print({fixture('memory_only.txt')!r})\n")
        result = self.run_cli("--doctor", "--no-color")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Boards: 1; dies: 1", result.stdout)
        self.assertIn("Memory types: MEMORY", result.stdout)

    def test_redirected_output_is_one_snapshot(self):
        self.write_driver(f"print({fixture('dual_die.txt')!r})\n")
        result = self.run_cli("--no-color")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 boards / 2 dies", result.stdout)
        self.assertIn("50.0%", result.stdout)
        self.assertNotIn("\x1b", result.stdout)

    def test_driver_failure_has_no_json_on_stdout(self):
        self.write_driver("import sys\nprint('Driver permission denied', file=sys.stderr)\nsys.exit(3)\n")
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Driver permission denied", result.stderr)

    def test_missing_driver_has_no_json_on_stdout(self):
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("npu-smi was not found", result.stderr)

    def test_hung_driver_is_bounded_and_reaped(self):
        pid_file = Path(self.directory.name) / "pid"
        self.write_driver(f"import os, pathlib, time\npathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeded 10 seconds", result.stderr)
        self.assertFalse(Path(f"/proc/{pid_file.read_text()}").exists())

    def test_invalid_intervals_fail_before_querying(self):
        for value in ("0", "-1", "0.49", "nan", "inf"):
            with self.subTest(value=value), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(["--interval", value])
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
