"""단위 시험: 레지스터 해석, RPM 계산, 판정 엔진, 설정 검증."""
import unittest

from inductrl import config as config_mod
from inductrl import decode
from inductrl.decision import DecisionEngine
from inductrl.models import (CH_CONDUCTIVITY, EV_CANDIDATE, EV_COUNTER_GAP, EV_COUNTER_RESET,
                             EV_HOLD_RESET, ST_COUNTER_GAP, ST_COUNTER_RESET, ST_ENDPOINT_INVALID,
                             ST_OK, ST_READ_ERROR, Reading)
from inductrl.rpm import RpmCalculator

from tests.helpers import example_cfg, verified_cfg


class DecodeTest(unittest.TestCase):
    def test_roundtrip_all_types(self) -> None:
        cases = [("uint16", 54321), ("int16", -1234), ("uint32", 3000000000), ("int32", -200000),
                 ("float32", 49.875)]
        for dtype, value in cases:
            for order in ("big", "little"):
                regs = decode.encode_registers(value, dtype, order)
                self.assertEqual(len(regs), decode.register_count(dtype))
                self.assertEqual(decode.decode_registers(regs, dtype, order), value, (dtype, order))

    def test_word_order_matters(self) -> None:
        self.assertEqual(decode.decode_registers([0x0001, 0x0002], "uint32", "big"), 0x00010002)
        self.assertEqual(decode.decode_registers([0x0001, 0x0002], "uint32", "little"), 0x00020001)

    def test_wrong_count_and_nan_raise(self) -> None:
        with self.assertRaises(decode.DecodeError):
            decode.decode_registers([1], "float32", "big")
        with self.assertRaises(decode.DecodeError):
            decode.decode_registers([0xFFFF, 0xFFFF], "float32", "big")  # NaN
        with self.assertRaises(decode.DecodeError):
            decode.decode_registers([1, 2], "int32", None)


class RpmTest(unittest.TestCase):
    PPR = 20
    DT = 0.5

    def _calc(self, bits: int = 16) -> RpmCalculator:
        return RpmCalculator(self.PPR, bits, 2.0, 600.0, max_endpoint_gap_s=1.0)

    def test_overflow_keeps_valid_rpm(self) -> None:
        calc = self._calc(16)
        count = 65536 - 120          # 곧 래핑
        pulses_per_step = 300 / 60.0 * self.PPR * self.DT  # 300 rpm → 50 펄스/0.5s
        wrapped = False
        for i in range(30):
            t = i * self.DT
            prev = count
            res = calc.update(t, count)
            self.assertIsNone(res.event)
            if t >= 2.0:
                self.assertEqual(res.status, ST_OK)
                self.assertAlmostEqual(res.value, 300.0, places=6)
            count = int((count + pulses_per_step) % 65536)
            if count < prev:
                wrapped = True
        self.assertTrue(wrapped)

    def test_reset_invalidates_window(self) -> None:
        calc = self._calc(16)
        count = 0
        reset_t = 5.0
        statuses = {}
        events = []
        for i in range(30):
            t = i * self.DT
            if abs(t - reset_t) < 1e-9:
                count = 0  # 리셋
            res = calc.update(t, count)
            statuses[t] = (res.status, res.value)
            if res.event is not None:
                events.append(res.event)
            count += 50 if t < reset_t - 1e-9 or t >= reset_t else 0
            if abs(t - (reset_t - self.DT)) < 1e-9:
                count = 9000  # 리셋 직전 값을 크게 만들어 둠
        self.assertEqual([e.event_type for e in events], [EV_COUNTER_RESET])
        for t in (5.0, 5.5, 6.0, 6.5):
            self.assertEqual(statuses[t][0], ST_COUNTER_RESET, t)
            self.assertIsNone(statuses[t][1])
        self.assertEqual(statuses[7.0][0], ST_OK)       # 구간 시작이 리셋 지점 이후
        self.assertAlmostEqual(statuses[8.0][1], 300.0, places=6)

    def test_invalid_endpoint_is_not_zero(self) -> None:
        calc = self._calc(16)
        for i in range(8):
            calc.update(i * self.DT, i * 50)
        res = calc.update(4.0, None)
        self.assertEqual(res.status, ST_ENDPOINT_INVALID)
        self.assertIsNone(res.value)
        # 구간 시작 샘플이 무효인 동안에도 무효
        for i in range(9, 12):
            res = calc.update(i * self.DT, i * 50)
        res = calc.update(6.0, 12 * 50)          # 구간 시작(4.0s) 샘플이 무효
        self.assertEqual(res.status, ST_ENDPOINT_INVALID)
        res = calc.update(6.5, 13 * 50)
        self.assertEqual(res.status, ST_OK)

    def test_long_gap_is_ambiguous(self) -> None:
        calc = RpmCalculator(self.PPR, 16, 2.0, 600.0, 1.0)
        calc.update(0.0, 100)
        res = calc.update(400.0, 200)  # 400 초 동안 최대 80000 펄스 > 65536
        self.assertEqual(res.event.event_type, EV_COUNTER_GAP)
        self.assertIn(res.status, (ST_COUNTER_GAP, "INSUFFICIENT_WINDOW"))
        self.assertIsNone(res.value)


def _reading(t: float, value: float, ok: bool = True) -> Reading:
    return Reading(CH_CONDUCTIVITY, t, "", "[0]", value if ok else None, "mS/cm",
                   ST_OK if ok else ST_READ_ERROR, None if ok else "x")


class DecisionEngineTest(unittest.TestCase):
    def _run(self, engine: DecisionEngine, until: float, bad_at=(), value: float = 50.0,
             rpm: float = 300.0):
        out = []
        t = 0.0
        while t <= until + 1e-9:
            ok = not any(abs(t - b) < 1e-9 for b in bad_at)
            res = engine.update(t, rpm, {CH_CONDUCTIVITY: _reading(t, value, ok)})
            out.append((t, res))
            t = round(t + 0.5, 6)
        return out

    def test_flat_values_wait_for_min_stir(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "launch", 0.0)
        out = self._run(engine, 40.0)
        cands = [t for t, r in out if r.candidate]
        self.assertEqual(cands, [33.0])   # min_stir 30 + hold 3
        self.assertTrue(all("min_stir" in r.reasons for t, r in out if t < 30.0))

    def test_interruption_resets_hold(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "launch", 0.0)
        out = self._run(engine, 40.0, bad_at=(31.5,))
        resets = [ev for t, r in out for ev in r.events if ev.event_type == EV_HOLD_RESET]
        self.assertEqual(len(resets), 1)
        self.assertAlmostEqual(resets[0].detail["held_s"], 1.5)
        res_at_bad = dict(out)[31.5]
        self.assertIsNone(res_at_bad.hold_s)
        self.assertEqual(dict(out)[32.0].hold_s, 0.0)
        self.assertEqual([t for t, r in out if r.candidate], [35.0])

    def test_coverage_shortfall_blocks(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "launch", 0.0)
        bad = [i * 2.0 for i in range(1, 40)]  # 4주기 중 1회 누락 → 75%
        out = self._run(engine, 60.0, bad_at=bad)
        self.assertFalse(any(r.candidate for _, r in out))
        self.assertTrue(any(any(x.startswith("coverage:") for x in r.reasons) for _, r in out))

    def test_out_of_range_never_candidate(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "launch", 0.0)
        out = self._run(engine, 60.0, value=70.0)
        self.assertFalse(any(r.candidate for _, r in out))
        self.assertIn("above_max:conductivity", out[-1][1].reasons)

    def test_not_running_blocks(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "launch", 0.0)
        out = self._run(engine, 60.0, rpm=20.0)
        self.assertFalse(any(r.candidate for _, r in out))
        self.assertIn("not_running", out[-1][1].reasons)

    def test_null_thresholds_mark_unverified(self) -> None:
        cfg = verified_cfg()
        cfg["decision"]["saltwater"]["conductivity_max_range"] = None
        engine = DecisionEngine(cfg, "saltwater", "launch", 0.0)
        out = self._run(engine, 40.0)
        cand = [ev for _, r in out for ev in r.events if ev.event_type == EV_CANDIDATE]
        self.assertEqual(len(cand), 1)
        self.assertFalse(cand[0].detail["verified"])
        self.assertIn("decision.saltwater.conductivity_max_range = null", cand[0].detail["unverified_reasons"])

    def test_rpm_trigger_needs_confirm_seconds(self) -> None:
        engine = DecisionEngine(verified_cfg(), "saltwater", "rpm", None)
        r1 = engine.update(0.0, 150.0, {})
        r2 = engine.update(1.5, 150.0, {})
        self.assertIsNone(engine.t0)
        r3 = engine.update(2.0, 150.0, {})
        self.assertEqual(engine.t0, 2.0)
        self.assertEqual(r3.events[0].event_type, "batch_started")
        self.assertEqual((r1.state, r2.state), ("WAITING_START", "WAITING_START"))


class ConfigTest(unittest.TestCase):
    def test_example_config_is_structurally_valid(self) -> None:
        self.assertEqual(config_mod.validate_config(example_cfg()), [])

    def test_example_has_unconfirmed_values_null(self) -> None:
        cfg = example_cfg()
        self.assertFalse(cfg["actuator"]["verified"])
        self.assertFalse(cfg["actuator"]["live_enabled"])
        self.assertIsNone(cfg["actuator"]["stop_value"])
        self.assertIsNone(cfg["actuator"]["stop_coil_address"])
        self.assertFalse(cfg["decision"]["thresholds_verified"])
        for ch in cfg["channels"].values():
            self.assertIsNone(ch["address_zero_based"])
            self.assertFalse(ch["mapping_verified"])

    def test_stop_value_must_be_bool(self) -> None:
        cfg = verified_cfg()
        cfg["actuator"]["stop_value"] = 1
        self.assertTrue(any("stop_value" in e for e in config_mod.validate_config(cfg)))

    def test_mapping_verified_requires_fields(self) -> None:
        cfg = example_cfg()
        cfg["channels"]["conductivity"]["mapping_verified"] = True
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("conductivity" in e and "address_zero_based" in e for e in errors))

    def test_budget_cannot_exceed_interval(self) -> None:
        cfg = example_cfg()
        cfg["cycle_budget_s"] = 0.6
        self.assertTrue(any("cycle_budget_s" in e for e in config_mod.validate_config(cfg)))


if __name__ == "__main__":
    unittest.main()
