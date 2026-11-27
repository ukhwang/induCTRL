"""mic/agent.py 단위 테스트 — 하드웨어·Neon 없이 명령 처리 규칙만 확인합니다.

    cd webapp/mic && python3 -m unittest discover -s tests -t .
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import Agent, build_stirctl_args, new_batch_id  # noqa: E402


class FakeQueue:
    def __init__(self, commands=None):
        self.commands = list(commands or [])
        self.finished = []
        self.heartbeats = []
        self.canceled = []
        self.recovered = 0

    def claim(self):
        command = self.commands.pop(0) if self.commands else None
        if command is not None:
            command = {**command, "created_at": None}
        return command

    def finish(self, command_id, status, error=None, result=None):
        self.finished.append({"id": command_id, "status": status, "error": error, "result": result or {}})

    def heartbeat(self, **fields):
        self.heartbeats.append(fields)

    def cancel_pending_starts(self, reason):
        self.canceled.append(reason)
        return 1

    def recover(self, max_age_s=120.0):
        return {"failed": self.recovered, "stale": 0, "stops": 0}

    def close(self):
        pass


class FakeRunner:
    def __init__(self, start_ok=True, exit_code=None):
        self.start_ok = start_ok
        self.exit_code = exit_code
        self.started = []
        self.stops = 0
        self.batch_id = None
        self.last_exit = None
        self._alive = False

    def alive(self):
        return self._alive

    def start(self, *, material, payload):
        self.started.append({"material": material, "payload": payload})
        if not self.start_ok:
            return {"ok": False, "error": "spawn_failed", "message": "python3 를 찾지 못했습니다"}
        self._alive = True
        self.batch_id = "live-{}-test".format(material)
        return {"ok": True, "batch_id": self.batch_id}

    def poll(self):
        if self.exit_code is None or not self._alive:
            return None
        self._alive = False
        info = {"code": self.exit_code, "signal": None}
        self.last_exit = info
        return info

    def stop(self, grace=8.0):
        self._alive = False
        self.stops += 1
        return {"stopped": True, "exit": {"code": 130, "signal": "SIGINT"}}


class FakePwm:
    def __init__(self, fail=False):
        self.speeds = []
        self.stops = 0
        self.closed = False
        self.enabled = True
        self.fail = fail

    def set_speed(self, pct):
        self.speeds.append(pct)
        return None if self.fail else pct

    def stop(self):
        self.stops += 1
        return True

    def close(self):
        self.closed = True


def make_agent(commands=None, clock=None, pwm_fail=False, **runner_kwargs):
    queue = FakeQueue(commands)
    runner = FakeRunner(**runner_kwargs)
    pwm = FakePwm(fail=pwm_fail)
    agent = Agent(queue=queue, runner=runner, pwm=pwm, mode="mock", db_path=None,
                  clock=clock or (lambda: 0.0), sleep=lambda _s: None, log=lambda _m: None,
                  heartbeat_seconds=5.0)
    agent.start()
    queue.heartbeats.clear()      # 시작 시 생존 신호는 따로 셉니다.
    return agent, queue, runner, pwm


class StirctlArgsTests(unittest.TestCase):
    def test_observe_mode_does_not_pass_mock_only_flags(self):
        args = build_stirctl_args(mode="observe", material="saltwater", batch_id="b1", db="/tmp/x.sqlite",
                                  payload={"water_l": 10, "solute_g": 500, "control_mode": "ai"})
        self.assertIn("--mode", args)
        self.assertEqual(args[args.index("--mode") + 1], "observe")
        self.assertNotIn("--water-l", args)
        self.assertNotIn("--arm-stop", args)

    def test_mock_mode_passes_charge_and_speed(self):
        args = build_stirctl_args(mode="mock", material="caco3", batch_id="b2", db="/tmp/x.sqlite",
                                  payload={"water_l": 12, "solute_g": 800, "control_mode": "fixed"},
                                  mock_speed=10)
        self.assertEqual(args[args.index("--water-l") + 1], "12")
        self.assertEqual(args[args.index("--solute-g") + 1], "800")
        self.assertEqual(args[args.index("--control-mode") + 1], "fixed")
        self.assertEqual(args[args.index("--speed-factor") + 1], "10")

    def test_live_mode_requires_both_flags(self):
        args = build_stirctl_args(mode="live", material="saltwater", batch_id="b3", db="/tmp/x.sqlite",
                                  arm_stop=True)
        self.assertIn("--arm-stop", args)

    def test_batch_id_is_readable(self):
        self.assertRegex(new_batch_id("saltwater"), r"^live-saltwater-\d{4}-\d{6}-[0-9a-f]{4}$")


class AgentTests(unittest.TestCase):
    def test_start_command_drives_pwm_first_then_stirctl(self):
        command = {"id": 7, "action": "START",
                   "payload": {"material": "saltwater", "water_l": 10, "solute_g": 500,
                               "control_mode": "rule", "target_rpm_pct": 65}}
        agent, queue, runner, pwm = make_agent([command])
        result = agent.tick()
        self.assertEqual(result["claimed"], "START")
        self.assertEqual(pwm.speeds, [65])
        self.assertEqual(len(runner.started), 1)
        self.assertEqual(agent.state, "running")
        self.assertEqual(queue.finished, [])
        self.assertEqual(agent.active_command["id"], 7)

    def test_second_start_is_rejected_while_running(self):
        first = {"id": 1, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 60}}
        second = {"id": 2, "action": "START", "payload": {"material": "caco3", "target_rpm_pct": 60}}
        agent, queue, runner, _pwm = make_agent([first, second])
        agent.tick()
        agent.tick()
        self.assertEqual(len(runner.started), 1)
        self.assertEqual(queue.finished[-1]["id"], 2)
        self.assertEqual(queue.finished[-1]["status"], "FAILED")
        self.assertIn("이미 교반", queue.finished[-1]["error"])

    def test_child_exit_completes_command_and_stops_motor(self):
        command = {"id": 3, "action": "START", "payload": {"material": "caco3", "target_rpm_pct": 70}}
        agent, queue, runner, pwm = make_agent([command], exit_code=0)
        agent.tick()
        stops_after_start = pwm.stops
        agent.tick()          # 자식이 끝난 것을 확인하는 주기
        self.assertEqual(queue.finished[-1]["id"], 3)
        self.assertEqual(queue.finished[-1]["status"], "COMPLETED")
        self.assertEqual(queue.finished[-1]["result"]["batch_id"], "live-caco3-test")
        self.assertEqual(agent.state, "idle")
        self.assertGreater(pwm.stops, stops_after_start)

    def test_crashed_stirctl_marks_failed(self):
        command = {"id": 4, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 60}}
        agent, queue, _runner, _pwm = make_agent([command], exit_code=1)
        agent.tick()
        agent.tick()
        self.assertEqual(queue.finished[-1]["status"], "FAILED")
        self.assertIn("종료 코드 1", queue.finished[-1]["error"])

    def test_stop_command_stops_motor_before_anything_else(self):
        start = {"id": 5, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 60}}
        stop = {"id": 6, "action": "STOP", "payload": {}}
        agent, queue, runner, pwm = make_agent([start, stop])
        agent.tick()
        pwm.stops = 0
        agent.tick()
        self.assertEqual(pwm.stops, 1)
        self.assertEqual(runner.stops, 1)
        self.assertEqual(queue.canceled, ["정지 요청으로 취소"])
        self.assertEqual([item["id"] for item in queue.finished], [5, 6])
        self.assertEqual(queue.finished[0]["status"], "COMPLETED")
        self.assertEqual(agent.state, "idle")

    def test_spawn_failure_releases_motor_and_reports(self):
        command = {"id": 8, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 60}}
        agent, queue, _runner, pwm = make_agent([command], start_ok=False)
        agent.tick()
        self.assertEqual(queue.finished[-1]["status"], "FAILED")
        self.assertIn("찾지 못했습니다", queue.finished[-1]["error"])
        self.assertGreaterEqual(pwm.stops, 2)   # 시작 시 1회 + 실패 후 1회
        self.assertEqual(agent.state, "idle")

    def test_pwm_failure_blocks_the_start(self):
        command = {"id": 11, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 70}}
        agent, queue, runner, pwm = make_agent([command], pwm_fail=True)
        agent.tick()
        self.assertEqual(len(runner.started), 0)
        self.assertEqual(queue.finished[-1]["status"], "FAILED")
        self.assertIn("PWM", queue.finished[-1]["error"])
        self.assertEqual(agent.state, "idle")

    def test_invalid_material_is_rejected(self):
        command = {"id": 9, "action": "START", "payload": {"material": "cement", "target_rpm_pct": 60}}
        agent, queue, runner, _pwm = make_agent([command])
        agent.tick()
        self.assertEqual(len(runner.started), 0)
        self.assertEqual(queue.finished[-1]["status"], "FAILED")

    def test_heartbeat_is_rate_limited(self):
        now = [10.0]
        agent, queue, _runner, _pwm = make_agent([], clock=lambda: now[0])
        queue.heartbeats.clear()
        agent.heartbeat(force=True)        # 10초: 전송
        now[0] = 13.0
        agent.tick()                       # 3초 뒤: 생략
        now[0] = 14.9
        agent.tick()                       # 5초 미만: 생략
        self.assertEqual(len(queue.heartbeats), 1)
        now[0] = 15.5
        agent.tick()                       # 5.5초 경과: 전송
        self.assertEqual(len(queue.heartbeats), 2)

    def test_shutdown_stops_motor_and_marks_device_stopped(self):
        agent, queue, runner, pwm = make_agent([
            {"id": 10, "action": "START", "payload": {"material": "saltwater", "target_rpm_pct": 60}},
        ])
        agent.tick()
        pwm.stops = 0
        agent.shutdown()
        self.assertEqual(pwm.stops, 1)
        self.assertEqual(runner.stops, 1)
        self.assertEqual(queue.heartbeats[-1]["state"], "stopped")
        self.assertTrue(agent.stopping)


if __name__ == "__main__":
    unittest.main()
