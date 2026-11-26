"""mock 시나리오 시험 (가상 시계, 수 초 안에 끝남)."""
import unittest
from unittest import mock

from inductrl.models import (EV_CANDIDATE, EV_COUNTER_RESET, EV_MODBUS_RESPONSE, EV_OVERRUN,
                             EV_STOP_CONFIRMED, EV_STOP_OBSERVED, EV_STOP_REQUESTED,
                             EV_STOP_UNCONFIRMED, FS_AUTO_STOP_CONFIRMED, FS_NO_CANDIDATE,
                             FS_STOP_UNCONFIRMED)

from tests.helpers import TempDirCase, example_cfg

STOP_SEQ = [EV_CANDIDATE, EV_STOP_REQUESTED, EV_MODBUS_RESPONSE, EV_STOP_CONFIRMED]


class NormalCompletionTest(TempDirCase):
    def _check_normal(self, material: str) -> None:
        bid = "normal-" + material
        summary = self.run_mock(material, "normal", 200, batch_id=bid)
        self.assertEqual(summary.final_status, FS_AUTO_STOP_CONFIRMED)
        types = [t for t in self.event_types(bid) if t in STOP_SEQ + [EV_STOP_UNCONFIRMED]]
        self.assertEqual(types, STOP_SEQ)
        ev = {t: self.first_event(bid, t) for t in STOP_SEQ}
        times = [ev[t]["monotonic_s"] for t in STOP_SEQ]
        self.assertEqual(times, sorted(times))
        self.assertEqual(ev[EV_CANDIDATE]["monotonic_s"], ev[EV_STOP_REQUESTED]["monotonic_s"])
        since = ev[EV_CANDIDATE]["detail"]["since_t0_s"]
        self.assertGreaterEqual(since, 30.0 + 3.0)
        dt_confirm = ev[EV_STOP_CONFIRMED]["monotonic_s"] - ev[EV_STOP_REQUESTED]["monotonic_s"]
        self.assertGreaterEqual(dt_confirm, 2.0)   # stop_hold_seconds
        self.assertLessEqual(dt_confirm, 10.0)     # stop_confirm_timeout_s
        self.assertEqual(ev[EV_MODBUS_RESPONSE]["detail"]["outcome"], "success")
        self.assertEqual(summary.mock_actuator.calls, [ev[EV_STOP_REQUESTED]["monotonic_s"]])
        # 후보 이후에도 수집 계속
        last_seq = self.rows("SELECT max(seq) FROM readings WHERE batch_id=?", (bid,))[0][0]
        self.assertEqual(last_seq, 199)

    def test_saltwater(self) -> None:
        self._check_normal("saltwater")

    def test_caco3(self) -> None:
        self._check_normal("caco3")

    def test_launch_trigger(self) -> None:
        self.run_mock("saltwater", "early_flat", 120, batch_id="launch", start_trigger="launch")
        started = self.first_event("launch", "batch_started")
        self.assertEqual(started["detail"]["trigger"], "launch")
        self.assertAlmostEqual(started["elapsed_s"], 0.0)


class NoCandidateTest(TempDirCase):
    def test_early_flat_waits_for_min_stir(self) -> None:
        self.run_mock("saltwater", "early_flat", 120, batch_id="flat")
        start = self.first_event("flat", "batch_started")["monotonic_s"]
        cand = self.first_event("flat", EV_CANDIDATE)
        self.assertAlmostEqual(cand["monotonic_s"] - start, 33.0, places=6)
        early = self.rows("SELECT count(*) FROM decision_log WHERE batch_id=? AND monotonic_s < ? "
                          "AND state IN ('HOLDING','CANDIDATE')", ("flat", start + 30.0))[0][0]
        self.assertEqual(early, 0)

    def test_out_of_range(self) -> None:
        s = self.run_mock("saltwater", "out_of_range", 160, batch_id="oor")
        self.assertNotIn(EV_CANDIDATE, self.event_types("oor"))
        self.assertEqual(s.final_status, FS_NO_CANDIDATE)
        self.assertEqual(s.mock_actuator.calls, [])

    def test_dropout_blocks_and_never_holds(self) -> None:
        self.run_mock("saltwater", "dropout", 160, batch_id="drop")
        self.assertNotIn(EV_CANDIDATE, self.event_types("drop"))
        n_cov = self.rows("SELECT count(*) FROM decision_log WHERE batch_id=? AND reasons_json LIKE "
                          "'%coverage:conductivity%'", ("drop",))[0][0]
        self.assertGreater(n_cov, 50)
        errs = self.rows("SELECT value FROM readings WHERE batch_id=? AND channel='conductivity' "
                         "AND status='READ_ERROR'", ("drop",))
        self.assertGreater(len(errs), 30)
        self.assertTrue(all(r[0] is None for r in errs))


class InvalidDataTest(TempDirCase):
    def test_stale_is_null_not_zero(self) -> None:
        self.run_mock("saltwater", "stale", 120, batch_id="stale")
        stale = self.rows("SELECT value, raw_json, elapsed_s FROM readings WHERE batch_id=? "
                          "AND channel='conductivity' AND status='STALE'", ("stale",))
        self.assertGreater(len(stale), 50)
        self.assertTrue(all(r["value"] is None and r["raw_json"] is None for r in stale))
        first_stale = min(r["elapsed_s"] for r in stale)
        self.assertGreater(first_stale, 20.0 + 2.0 - 0.01)  # stale_seconds=2 경과 후
        zeros = self.rows("SELECT count(*) FROM readings WHERE batch_id=? AND channel='conductivity' "
                          "AND status<>'OK' AND value IS NOT NULL", ("stale",))[0][0]
        self.assertEqual(zeros, 0)
        self.assertNotIn(EV_CANDIDATE, self.event_types("stale"))

    def test_out_of_range_value_is_null(self) -> None:
        self.run_mock("caco3", "invalid_value", 120, batch_id="bad")
        rows = self.rows("SELECT value, raw_json, error_detail FROM readings WHERE batch_id=? "
                         "AND status='OUT_OF_RANGE'", ("bad",))
        self.assertGreater(len(rows), 20)
        for r in rows:
            self.assertIsNone(r["value"])
            self.assertIsNotNone(r["raw_json"])   # 원시값은 남김
            self.assertIn("유효범위", r["error_detail"])


class CounterTest(TempDirCase):
    def test_overflow_keeps_rpm(self) -> None:
        s = self.run_mock("saltwater", "counter_overflow", 200, batch_id="ovf")
        self.assertNotIn(EV_COUNTER_RESET, self.event_types("ovf"))
        counts = [int(r[0]) for r in self.rows(
            "SELECT value FROM readings WHERE batch_id=? AND channel='rotation_counter' ORDER BY seq", ("ovf",))]
        self.assertTrue(any(b < a for a, b in zip(counts, counts[1:])), "래핑이 일어나야 함")
        stop_t = self.first_event("ovf", EV_STOP_REQUESTED)["elapsed_s"]
        rpms = self.rows("SELECT value, status FROM readings WHERE batch_id=? AND channel='rpm' "
                         "AND elapsed_s BETWEEN 6 AND ?", ("ovf", stop_t))
        self.assertTrue(all(r["status"] == "OK" for r in rpms))
        self.assertTrue(all(285.0 <= r["value"] <= 315.0 for r in rpms))
        self.assertEqual(s.final_status, FS_AUTO_STOP_CONFIRMED)

    def test_reset_invalidates_rpm_window(self) -> None:
        s = self.run_mock("saltwater", "counter_reset", 200, batch_id="rst")
        resets = [e for e in self.events("rst") if e["event_type"] == EV_COUNTER_RESET]
        self.assertEqual(len(resets), 1)
        t_reset = resets[0]["elapsed_s"]
        inside = self.rows("SELECT value, status FROM readings WHERE batch_id=? AND channel='rpm' "
                           "AND elapsed_s >= ? AND elapsed_s < ?", ("rst", t_reset, t_reset + 2.0))
        self.assertEqual(len(inside), 4)
        self.assertTrue(all(r["status"] == "COUNTER_RESET" and r["value"] is None for r in inside))
        after = self.rows("SELECT value, status FROM readings WHERE batch_id=? AND channel='rpm' "
                          "AND elapsed_s BETWEEN ? AND ?", ("rst", t_reset + 2.0, t_reset + 6.0))
        self.assertTrue(all(r["status"] == "OK" for r in after))
        self.assertEqual(s.final_status, FS_AUTO_STOP_CONFIRMED)


class StopFailureTest(TempDirCase):
    def _assert_unconfirmed(self, bid: str, reason: str) -> None:
        types = self.event_types(bid)
        self.assertNotIn(EV_STOP_CONFIRMED, types)
        unconf = self.first_event(bid, EV_STOP_UNCONFIRMED)
        self.assertEqual(unconf["detail"]["reason"], reason)
        self.assertEqual(types.count(EV_STOP_REQUESTED), 1)

    def test_write_exception(self) -> None:
        s = self.run_mock("saltwater", "write_exception", 200, batch_id="wex")
        self._assert_unconfirmed("wex", "write_exception_response")
        self.assertEqual(len(s.mock_actuator.calls), 1)   # 재시도 0회
        self.assertEqual(s.final_status, FS_STOP_UNCONFIRMED)
        resp = self.first_event("wex", EV_MODBUS_RESPONSE)
        unconf = self.first_event("wex", EV_STOP_UNCONFIRMED)
        self.assertEqual(resp["monotonic_s"], unconf["monotonic_s"])  # 즉시 기록

    def test_write_timeout_then_observed(self) -> None:
        s = self.run_mock("saltwater", "write_timeout", 200, batch_id="wto")
        self._assert_unconfirmed("wto", "write_timeout")
        self.assertEqual(len(s.mock_actuator.calls), 1)
        self.assertIn(EV_STOP_OBSERVED, self.event_types("wto"))
        self.assertEqual(s.final_status, FS_STOP_UNCONFIRMED)  # 관측됐어도 '자동 정지 확인' 아님

    def test_write_connection_error(self) -> None:
        s = self.run_mock("saltwater", "write_connection_error", 200, batch_id="wce")
        self._assert_unconfirmed("wce", "write_connection_error")
        self.assertEqual(s.final_status, FS_STOP_UNCONFIRMED)

    def test_success_but_rpm_not_dropping(self) -> None:
        s = self.run_mock("saltwater", "no_rpm_drop", 200, batch_id="nod")
        self._assert_unconfirmed("nod", "confirm_timeout")
        req = self.first_event("nod", EV_STOP_REQUESTED)["monotonic_s"]
        unconf = self.first_event("nod", EV_STOP_UNCONFIRMED)["monotonic_s"]
        self.assertGreater(unconf - req, 10.0)
        self.assertLessEqual(unconf - req, 10.0 + 0.5 + 1e-6)
        self.assertEqual(s.final_status, FS_STOP_UNCONFIRMED)

    def test_single_stop_request_per_batch(self) -> None:
        # 모터가 계속 돌아 조건이 계속 충족되어도 정지 요청은 1회
        s = self.run_mock("saltwater", "no_rpm_drop", 240, batch_id="one")
        self.assertEqual(self.event_types("one").count(EV_STOP_REQUESTED), 1)
        self.assertEqual(self.event_types("one").count(EV_CANDIDATE), 1)
        self.assertEqual(len(s.mock_actuator.calls), 1)
        met_after = self.rows("SELECT count(*) FROM decision_log WHERE batch_id=? AND "
                              "state='MET_AFTER_CANDIDATE'", ("one",))[0][0]
        self.assertGreater(met_after, 20)


class BudgetTest(TempDirCase):
    def test_slow_reads_skip_remaining(self) -> None:
        self.run_mock("saltwater", "slow_reads", 20, batch_id="slow")
        skipped = self.rows("SELECT channel FROM readings WHERE batch_id=? AND status='SKIPPED_BUDGET'",
                            ("slow",))
        self.assertEqual(len(skipped), 20)       # 4번째 채널(0.45s 시점)이 매 주기 생략
        self.assertEqual({r[0] for r in skipped}, {"turbidity_bottom"})
        self.assertEqual(self.event_types("slow").count(EV_OVERRUN), 20)


class MockHasNoModbusTest(TempDirCase):
    def test_no_modbus_client_created(self) -> None:
        with mock.patch("inductrl.modbus_io.create_tcp_client",
                        side_effect=AssertionError("읽기 클라이언트 생성됨")) as rc, \
                mock.patch("inductrl.actuator.create_write_client",
                           side_effect=AssertionError("쓰기 클라이언트 생성됨")) as wc:
            s = self.run_mock("saltwater", "normal", 200, batch_id="nomb")
        rc.assert_not_called()
        wc.assert_not_called()
        self.assertEqual(s.final_status, FS_AUTO_STOP_CONFIRMED)

    def test_example_config_mock_fills_are_recorded(self) -> None:
        import json
        self.run_mock("saltwater", "normal", 10, batch_id="fill")
        cfg = json.loads(self.rows("SELECT config_json FROM batches WHERE batch_id='fill'")[0][0])
        self.assertIn("channels.conductivity.address_zero_based", cfg["_mock_filled"])
        self.assertFalse(cfg["decision"]["thresholds_verified"])  # 가상값이 검증 플래그를 켜지 않음
        self.assertEqual(example_cfg()["channels"]["conductivity"]["address_zero_based"], None)


if __name__ == "__main__":
    unittest.main()
