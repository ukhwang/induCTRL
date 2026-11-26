"""재생, 수동 입력 CLI, DB 설정, 기존 코드 호환, 쓰기 코드 위치 시험."""
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from inductrl import cli
from inductrl.replay import format_report, recompute, replay_report

from tests.helpers import ROOT, TempDirCase, example_cfg


class ReplayTest(TempDirCase):
    def _assert_match(self, bid: str) -> dict:
        con = self.con()
        try:
            rep = replay_report(con, bid)
        finally:
            con.close()
        self.assertTrue(rep["candidate_match"], rep)
        orig = self.first_event(bid, "candidate")
        if orig is not None:
            self.assertEqual(rep["recomputed"]["candidate"]["monotonic_s"], orig["monotonic_s"])
        self.assertIn("후보 시각 일치: 예", format_report(rep))
        return rep

    def test_replay_matches_normal(self) -> None:
        self.run_mock("saltwater", "normal", 200, batch_id="r1")
        rep = self._assert_match("r1")
        self.assertIsNotNone(rep["recomputed"]["candidate"])
        self.assertIn("stop_confirmed", rep["original_events"])

    def test_replay_matches_caco3_launch(self) -> None:
        self.run_mock("caco3", "normal", 200, batch_id="r2", start_trigger="launch")
        self._assert_match("r2")

    def test_replay_matches_counter_reset_and_gaps(self) -> None:
        self.run_mock("saltwater", "counter_reset", 200, batch_id="r3")
        rep = self._assert_match("r3")
        self.assertEqual(rep["recomputed"]["counter_resets"], 1)
        self.run_mock("saltwater", "stale", 120, batch_id="r4")
        self._assert_match("r4")
        self.run_mock("saltwater", "slow_reads", 120, batch_id="r5")
        self._assert_match("r5")

    def test_alternative_config_compared(self) -> None:
        self.run_mock("saltwater", "early_flat", 150, batch_id="r6")
        alt = example_cfg()
        alt["decision"]["hold_seconds"] = 1.0  # 원래 실행은 38s에 가상 정지 → 더 긴 유지는 재현 불가
        con = self.con()
        try:
            rep = replay_report(con, "r6", alt)
            base = recompute(con, "r6")
        finally:
            con.close()
        diff = rep["alternative"]["candidate"]["elapsed_s"] - base["candidate"]["elapsed_s"]
        self.assertAlmostEqual(diff, -2.0, places=6)
        self.assertIn("다른 설정", format_report(rep))

    def test_replay_cli_with_manual_entries(self) -> None:
        self.run_mock("saltwater", "normal", 120, batch_id="r7")
        db = ["--db", self.db_path]
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["sample", "add", "--batch-id", "r7", "--position", "mid",
                                       "--metric", "temperature", "--value", "21.5", "--unit", "C",
                                       "--measured-at", "2026-12-01T05:03:05Z"] + db), 0)
            self.assertEqual(cli.main(["quality", "set", "--batch-id", "r7", "--passed", "no",
                                       "--note", "하단 침전"] + db), 0)
            self.assertEqual(cli.main(["manual-stop", "--batch-id", "r7",
                                       "--at", "2026-12-01T14:03:30+09:00"] + db), 0)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(["replay", "--batch-id", "r7"] + db), 0)
        text = out.getvalue()
        self.assertIn("temperature = 21.5 C", text)
        self.assertIn("불합격", text)
        self.assertIn("2026-12-01T05:03:30+00:00", text)   # +09:00 → UTC 변환
        self.assertIn("후보 시각 일치: 예", text)


class ManualCliTest(TempDirCase):
    def test_wal_and_busy_timeout(self) -> None:
        con = self.con()
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 1000)
        finally:
            con.close()

    def test_batch_show_and_errors(self) -> None:
        self.run_mock("saltwater", "write_exception", 200, batch_id="s1")
        db = ["--db", self.db_path]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(["batch", "show", "--batch-id", "s1"] + db), 0)
        text = out.getvalue()
        self.assertIn("STOP_UNCONFIRMED", text)
        self.assertIn("stop_unconfirmed", text)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(["manual-stop", "--batch-id", "nope"] + db), 2)
            self.assertEqual(cli.main(["sample", "add", "--batch-id", "s1", "--position", "top",
                                       "--metric", "x", "--value", "1", "--measured-at", "어제"] + db), 2)

    def test_duplicate_batch_id_refused(self) -> None:
        self.run_mock("saltwater", "normal", 5, batch_id="dup")
        from inductrl.runner import BatchExistsError
        with self.assertRaises(BatchExistsError):
            self.run_mock("saltwater", "normal", 5, batch_id="dup")


class LegacyAndStaticTest(unittest.TestCase):
    def test_existing_test_local_passes(self) -> None:
        result = subprocess.run([sys.executable, os.path.join(ROOT, "test_local.py")], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_modbus_writes_only_in_actuator(self) -> None:
        pkg = os.path.join(ROOT, "inductrl")
        pattern = re.compile(r"\.write_[a-z_]*\s*\(")
        offenders = []
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py") or name == "actuator.py":
                continue
            with open(os.path.join(pkg, name), encoding="utf-8") as f:
                if pattern.search(f.read()):
                    offenders.append(name)
        self.assertEqual(offenders, [])
        with open(os.path.join(pkg, "actuator.py"), encoding="utf-8") as f:
            calls = pattern.findall(f.read())
        self.assertEqual(len(calls), 1)  # write_coil 단 한 곳

    def test_no_start_or_release_code(self) -> None:
        pkg = os.path.join(ROOT, "inductrl")
        forbidden = re.compile(r"def\s+(start_motor|restart|release|reset_do|clear_stop)\b")
        for name in os.listdir(pkg):
            if name.endswith(".py"):
                with open(os.path.join(pkg, name), encoding="utf-8") as f:
                    self.assertIsNone(forbidden.search(f.read()), name)


if __name__ == "__main__":
    unittest.main()
