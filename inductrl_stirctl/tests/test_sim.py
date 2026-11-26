"""실시간 시뮬레이터(물·용질 → 혼합·동력·에너지) 시험.

가상 시계로 돌기 때문에 실제 대기 없이 수 초 안에 끝납니다. Modbus 는 쓰지 않습니다.
"""
import io
import unittest

from inductrl.models import (EV_CANDIDATE, EV_STOP_BLOCKED, EV_STOP_CONFIRMED, EV_STOP_REQUESTED,
                               FS_AUTO_STOP_CONFIRMED, FS_CANDIDATE_UNVERIFIED)
from inductrl.runner import BatchRunner, RunOptions
from inductrl.sim import SimParams, fixed_baseline_energy_wh, mix_rate, power_w, validate
from inductrl.timeutil import SimClock, ScaledClock

from tests.helpers import TempDirCase, example_cfg


class SimParamsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        p = SimParams()
        self.assertEqual(validate(p), [])
        self.assertEqual(p.control_mode, "rule")
        self.assertAlmostEqual(p.concentration, 50.0)
        self.assertAlmostEqual(p.load, 1.0)
        self.assertAlmostEqual(p.njs_rpm, 190.0)

    def test_rejects_out_of_range(self) -> None:
        self.assertTrue(validate(SimParams(water_l=0.0)))
        self.assertTrue(validate(SimParams(solute_g=-1.0)))
        self.assertTrue(validate(SimParams(speed_factor=0.0)))
        self.assertTrue(validate(SimParams(control_mode="auto")))
        self.assertTrue(validate(SimParams(water_l=float("nan"))))
        self.assertEqual(validate(SimParams(water_l=30.0, solute_g=2000.0, speed_factor=20.0,
                                            control_mode="ai")), [])

    def test_load_grows_with_concentration(self) -> None:
        low = SimParams(water_l=20.0, solute_g=200.0)
        high = SimParams(water_l=10.0, solute_g=1500.0)
        self.assertGreater(high.load, low.load)
        self.assertGreater(high.njs_rpm, low.njs_rpm)
        self.assertGreater(high.tau_mix_s, low.tau_mix_s)

    def test_power_scales_with_rpm_and_load(self) -> None:
        self.assertEqual(power_w(0.0, 10.0, 1.0, 1.0), 0.0)
        self.assertGreater(power_w(320.0, 10.0, 1.0, 1.0), power_w(210.0, 10.0, 1.0, 1.0))
        self.assertGreater(power_w(300.0, 10.0, 1.6, 1.0), power_w(300.0, 10.0, 1.0, 1.0))
        self.assertGreater(power_w(300.0, 20.0, 1.0, 1.0), power_w(300.0, 10.0, 1.0, 1.0))

    def test_below_suspension_speed_does_not_mix(self) -> None:
        self.assertEqual(mix_rate(100.0, 190.0), 0.0)
        self.assertGreater(mix_rate(320.0, 190.0), 0.0)


class ScaledClockTest(unittest.TestCase):
    def test_simulated_seconds_compress_real_time(self) -> None:
        ticks = [0.0]
        slept = []

        def clock() -> float:
            return ticks[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            ticks[0] += seconds

        sc = ScaledClock(10.0, clock, sleep)
        start = sc.now()
        sc.sleep(5.0)                       # 설비 5초
        self.assertAlmostEqual(slept[0], 0.5)   # 실제로는 0.5초만 잔다
        self.assertAlmostEqual(sc.now() - start, 5.0)


class SimRunTest(TempDirCase):
    def _run(self, mode: str, material: str = "saltwater", water_l: float = 10.0,
             solute_g: float = 500.0, batch_id: str = None, samples: int = 0):
        sim = SimClock()
        params = SimParams(water_l=water_l, solute_g=solute_g, control_mode=mode)
        opts = RunOptions(mode="mock", material=material,
                          batch_id=batch_id or "{}-{}-{}".format(mode, material, water_l),
                          samples=samples, db_path=self.db_path, status_every=0, sim=params,
                          stop_grace_s=None if mode == "fixed" else 6.0, fast=True)
        return BatchRunner(example_cfg(), opts, sim.now, sim.sleep, io.StringIO(),
                           io.StringIO()).run()

    def _metrics(self, batch_id: str):
        return self.rows("SELECT * FROM sim_metrics WHERE batch_id=? ORDER BY seq", (batch_id,))

    def test_rule_stops_on_stability_and_records_metrics(self) -> None:
        summary = self._run("rule", batch_id="r1")
        self.assertEqual(summary.final_status, FS_AUTO_STOP_CONFIRMED)
        self.assertIn(EV_CANDIDATE, self.event_types("r1"))
        self.assertIn(EV_STOP_CONFIRMED, self.event_types("r1"))
        rows = self._metrics("r1")
        self.assertGreater(len(rows), 50)
        energies = [r["energy_wh"] for r in rows]
        self.assertEqual(energies, sorted(energies))          # 누적 에너지는 줄지 않음
        self.assertGreater(energies[-1], 0.1)
        self.assertLess(rows[-1]["cv_pct"], rows[0]["cv_pct"])  # 섞일수록 불균일도 감소
        run = self.rows("SELECT * FROM sim_runs WHERE batch_id='r1'")[0]
        self.assertEqual(run["control_mode"], "rule")
        self.assertAlmostEqual(run["concentration_g_per_l"], 50.0)
        self.assertGreater(run["baseline_energy_wh"], energies[-1] * 3)

    def test_ai_uses_less_energy_than_rule(self) -> None:
        rule = self._run("rule", batch_id="cmp-rule")
        ai = self._run("ai", batch_id="cmp-ai")
        rule_wh = self._metrics("cmp-rule")[-1]["energy_wh"]
        ai_wh = self._metrics("cmp-ai")[-1]["energy_wh"]
        self.assertEqual(rule.final_status, ai.final_status)
        self.assertLess(ai_wh, rule_wh * 0.75)
        phases = {r["phase"] for r in self._metrics("cmp-ai")}
        self.assertEqual(phases, {"DIAGNOSE", "SUSPEND", "CRUISE"})

    def test_fixed_runs_full_duration_without_stop(self) -> None:
        summary = self._run("fixed", batch_id="f1", samples=0)
        types = self.event_types("f1")
        self.assertEqual(summary.final_status, FS_CANDIDATE_UNVERIFIED)
        self.assertNotIn(EV_STOP_REQUESTED, types)
        self.assertIn(EV_STOP_BLOCKED, types)
        rows = self._metrics("f1")
        self.assertEqual(len(rows), 2 * int(SimParams().fixed_seconds))
        last = rows[-1]
        self.assertGreater(last["elapsed_s"], SimParams().fixed_seconds - 1.0)
        baseline = self.rows("SELECT baseline_energy_wh FROM sim_runs WHERE batch_id='f1'")[0][0]
        self.assertAlmostEqual(last["energy_wh"], baseline, delta=baseline * 0.05)

    def test_heavier_charge_takes_longer(self) -> None:
        light = self._run("ai", water_l=20.0, solute_g=200.0, batch_id="light")
        heavy = self._run("ai", water_l=5.0, solute_g=1500.0, batch_id="heavy")
        light_stop = self.first_event("light", EV_STOP_CONFIRMED)["elapsed_s"]
        heavy_stop = self.first_event("heavy", EV_STOP_CONFIRMED)["elapsed_s"]
        self.assertEqual(light.final_status, FS_AUTO_STOP_CONFIRMED)
        self.assertEqual(heavy.final_status, FS_AUTO_STOP_CONFIRMED)
        self.assertGreater(heavy_stop, light_stop)
        self.assertGreater(self._metrics("heavy")[-1]["energy_wh"],
                           self._metrics("light")[-1]["energy_wh"])

    def test_rule_speed_below_suspension_is_flagged(self) -> None:
        """진한 투입에서 고정 회전수(RULE)가 부상 임계보다 느리면 섞이지 않고 경고를 남깁니다."""
        summary = self._run("rule", water_l=5.0, solute_g=1500.0, batch_id="stall")
        self.assertEqual(summary.final_status, "NO_CANDIDATE")
        self.assertNotIn(EV_STOP_CONFIRMED, self.event_types("stall"))
        warn = self.first_event("stall", "warning")
        self.assertEqual(warn["detail"]["channel"], "sim")
        rows = self._metrics("stall")
        self.assertEqual(rows[-1]["phase"], "STALLED")
        self.assertLess(rows[-1]["mix"], 0.01)

    def test_sensor_signals_start_out_of_band(self) -> None:
        self._run("rule", batch_id="sig")
        first = self.rows("SELECT channel, value FROM readings WHERE batch_id='sig' AND seq=0 "
                          "AND channel IN ('conductivity','turbidity_top') ORDER BY channel")
        values = {r[0]: r[1] for r in first}
        self.assertGreater(values["conductivity"], 55.0)      # 처음엔 상단 농도가 몰려 있음
        self.assertLess(values["turbidity_top"], 400.0)       # 탄산칼슘은 하부에 침전

    def test_caco3_mixes_and_stops(self) -> None:
        summary = self._run("ai", material="caco3", batch_id="c1")
        self.assertEqual(summary.final_status, FS_AUTO_STOP_CONFIRMED)
        rows = self._metrics("c1")
        self.assertGreater(rows[-1]["energy_wh"], 0.1)
        self.assertLess(rows[-1]["cv_pct"], rows[0]["cv_pct"])

    def test_baseline_matches_fixed_energy_model(self) -> None:
        params = SimParams(water_l=20.0, solute_g=1200.0, control_mode="fixed")
        self._run("fixed", water_l=20.0, solute_g=1200.0, batch_id="f2")
        baseline = fixed_baseline_energy_wh(params)
        actual = self._metrics("f2")[-1]["energy_wh"]
        self.assertAlmostEqual(actual, baseline, delta=baseline * 0.05)


if __name__ == "__main__":
    unittest.main()
