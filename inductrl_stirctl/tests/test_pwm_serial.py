"""아두이노 PWM 시리얼 모듈 테스트.

하드웨어도 pyserial 도 필요 없습니다. 전송 인터페이스(`SerialTransport`)를
스크립트로 흉내내 프로토콜·검증·램프·정지를 확인합니다.
"""
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, List, Optional

from inductrl import pwm_serial as pwm
from inductrl.actuator import StopActuator

try:  # 테스트 환경에 pyserial 이 있으면 '설치 안내' 테스트는 건너뜁니다.
    import serial as _pyserial  # noqa: F401
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_CONFIG = os.path.join(ROOT, "config.example.json")


class ScriptedTransport(pwm.SerialTransport):
    """정해진 응답을 순서대로 돌려주고, 보낸 명령을 기록하는 가짜 전송."""

    description = "scripted"

    def __init__(self, lines: Optional[List[Optional[str]]] = None, fail_send: bool = False) -> None:
        self.lines = list(lines or [])
        self.fail_send = fail_send
        self.sent = []  # type: List[str]
        self.opened = False
        self.closed = False

    def open(self) -> None:
        if self.opened:
            raise pwm.PwmTransportError("이미 열림")
        self.opened = True

    def send_line(self, text: str) -> None:
        if self.fail_send:
            raise pwm.PwmTransportError("가상 전송 실패")
        self.sent.append(text)

    def read_line(self, timeout_s: float) -> Optional[str]:
        if not self.lines:
            return None
        return self.lines.pop(0)

    def close(self) -> None:
        self.closed = True
        self.opened = False


def make_controller(transport: pwm.SerialTransport, **kwargs: Any) -> pwm.PwmController:
    kwargs.setdefault("reset_wait_s", 0.0)
    return pwm.PwmController(transport, sleep=lambda seconds: None, **kwargs)


class ValueValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = ScriptedTransport(["OK PONG"])
        self.ctl = make_controller(self.transport)
        self.ctl.connect()

    def test_pct_range_and_type(self) -> None:
        for bad in (-1, 101, 1000, True, False, "60", None, 60.5):
            with self.assertRaises(ValueError):
                self.ctl.set_speed_pct(bad)  # type: ignore[arg-type]
        self.assertEqual(self.transport.sent, ["PING"], "잘못된 값은 전송되면 안 됩니다")

    def test_pct_accepts_bounds_and_integer_float(self) -> None:
        self.transport.lines.extend(["OK PCT 0", "OK PCT 100", "OK PCT 60"])
        self.assertEqual(self.ctl.set_speed_pct(0), 0)
        self.assertEqual(self.ctl.set_speed_pct(100), 100)
        self.assertEqual(self.ctl.set_speed_pct(60.0), 60)
        self.assertEqual(self.transport.sent[1:], ["SET_PCT 0", "SET_PCT 100", "SET_PCT 60"])

    def test_pwm_range(self) -> None:
        for bad in (-1, 256, True, "12"):
            with self.assertRaises(ValueError):
                self.ctl.set_pwm(bad)  # type: ignore[arg-type]

    def test_ramp_step_and_interval_validation(self) -> None:
        for step in (0, -5, 256, True):
            with self.assertRaises(ValueError):
                self.ctl.ramp_speed_pct(0, 50, step_pct=step)  # type: ignore[arg-type]
        for interval in (-0.1, float("nan"), "1"):
            with self.assertRaises(ValueError):
                self.ctl.ramp_speed_pct(0, 50, step_interval_s=interval)  # type: ignore[arg-type]

    def test_controller_rejects_foreign_transport(self) -> None:
        with self.assertRaises(TypeError):
            pwm.PwmController(object())  # type: ignore[arg-type]

    def test_transport_rejects_embedded_newline(self) -> None:
        device = pwm.SimulatedTransport()
        device.open()
        for bad in ("SET_PCT 10\nSTOP", "SET_PCT\r10", "   "):
            with self.assertRaises(ValueError):
                device.send_line(bad)
        self.assertEqual(device.sent, [])


class ProtocolFrameTest(unittest.TestCase):
    def test_each_command_is_one_line_ascii(self) -> None:
        transport = ScriptedTransport(["OK PONG", "OK PCT 60", "OK PWM 128", "OK STOPPED",
                                       "OK PONG", "OK HEARTBEAT", "OK STATUS pwm=0 pct=0"])
        ctl = make_controller(transport)
        ctl.connect()
        ctl.set_speed_pct(60)
        ctl.set_pwm(128)
        ctl.stop()
        ctl.ping()
        ctl.heartbeat()
        ctl.status()
        self.assertEqual(transport.sent, ["PING", "SET_PCT 60", "SET_PWM 128", "STOP",
                                          "PING", "HEARTBEAT", "STATUS"])
        for line in transport.sent:
            self.assertNotIn("\n", line)
            line.encode("ascii")  # ASCII 로 인코딩되어야 합니다

    def test_exchange_requires_connect(self) -> None:
        ctl = make_controller(ScriptedTransport(["OK PONG"]))
        with self.assertRaises(pwm.PwmTransportError):
            ctl.ping()
        self.assertEqual(ctl.frames_sent, 0)


class ResponseHandlingTest(unittest.TestCase):
    def test_ack_value_matches_request(self) -> None:
        transport = ScriptedTransport(["OK PONG", "OK PCT 61"])
        ctl = make_controller(transport)
        ctl.connect()
        with self.assertRaises(pwm.PwmProtocolError):
            ctl.set_speed_pct(60)  # 장치가 다른 값을 확인하면 오류

    def test_error_line_raises_command_error(self) -> None:
        transport = ScriptedTransport(["OK PONG", "ERR RANGE SET_PCT 0..100"])
        ctl = make_controller(transport)
        ctl.connect()
        with self.assertRaises(pwm.PwmCommandError) as ctx:
            ctl.set_speed_pct(60)  # 값은 범위 안이지만 장치가 거부하는 상황
        self.assertIn("ERR RANGE", ctx.exception.line)

    def test_timeout_raises(self) -> None:
        transport = ScriptedTransport(["OK PONG"])
        ctl = make_controller(transport, command_timeout_s=0.05)
        ctl.connect()
        with self.assertRaises(pwm.PwmTimeoutError):
            ctl._exchange("STATUS")  # noqa: SLF001
        self.assertEqual(ctl.frames_sent, 2)

    def test_unsolicited_lines_are_kept_as_events(self) -> None:
        transport = ScriptedTransport(["BOOT pwm=0 ready=1", "EVT WATCHDOG_STOP pwm=0",
                                       "OK PONG", "OK PONG"])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertTrue(ctl.ping())
        self.assertEqual(ctl.events, ["BOOT pwm=0 ready=1", "EVT WATCHDOG_STOP pwm=0"])

    def test_blank_lines_are_skipped(self) -> None:
        transport = ScriptedTransport(["", "", "OK PONG"])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertTrue(ctl.connected)

    def test_garbage_line_is_not_treated_as_ok(self) -> None:
        transport = ScriptedTransport(["hello", "OK PONG"])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertTrue(ctl.connected)
        self.assertEqual(ctl.events, ["hello"])


class StatusParsingTest(unittest.TestCase):
    def test_full_status_line(self) -> None:
        status = pwm.PwmStatus.from_line(
            "OK STATUS pwm=153 pct=60 target_pwm=178 target_pct=70 uptime_ms=1234 ramp=1 en=1")
        self.assertEqual((status.pwm, status.pct), (153, 60))
        self.assertEqual((status.target_pwm, status.target_pct), (178, 70))
        self.assertEqual(status.uptime_ms, 1234)
        self.assertEqual((status.ramp, status.en), (1, 1))
        self.assertEqual(status.as_dict()["pwm"], 153)

    def test_missing_pct_is_computed_from_pwm(self) -> None:
        status = pwm.PwmStatus.from_line("OK STATUS pwm=255")
        self.assertEqual(status.pct, 100)
        status = pwm.PwmStatus.from_line("OK STATUS pwm=0")
        self.assertEqual(status.pct, 0)

    def test_bad_status_lines(self) -> None:
        for line in ("OK PONG", "OK STATUS", "OK STATUS pwm=abc", "OK STATUS pwm=300",
                     "OK STATUS pwm=10 pct=200", "OK STATUS pwm=10 uptime_ms=x"):
            with self.assertRaises(pwm.PwmProtocolError, msg=line):
                pwm.PwmStatus.from_line(line)

    def test_describe_contains_required_fields(self) -> None:
        text = pwm.PwmStatus(pwm=51, pct=20).describe()
        self.assertIn("pwm=51", text)
        self.assertIn("pct=20", text)


class ConnectTest(unittest.TestCase):
    def test_reset_wait_then_ping(self) -> None:
        transport = ScriptedTransport(["OK PONG"])
        sleeps = []  # type: List[float]
        ctl = pwm.PwmController(transport, reset_wait_s=2.0, sleep=sleeps.append)
        ctl.connect()
        self.assertEqual(transport.sent, ["PING"])
        self.assertEqual(sleeps[0], 2.0)
        self.assertTrue(ctl.connected)

    def test_ping_retries_over_boot_reset(self) -> None:
        transport = ScriptedTransport([None, None, "OK PONG"])
        sleeps = []  # type: List[float]
        ctl = pwm.PwmController(transport, reset_wait_s=0.0, ping_attempts=4,
                                sleep=sleeps.append)
        ctl.connect()
        self.assertEqual(transport.sent, ["PING", "PING", "PING"])

    def test_connect_gives_up_with_timeout(self) -> None:
        transport = ScriptedTransport([None, None, None])
        ctl = pwm.PwmController(transport, reset_wait_s=0.0, ping_attempts=3,
                                sleep=lambda seconds: None)
        with self.assertRaises(pwm.PwmTimeoutError):
            ctl.connect()
        self.assertEqual(len(transport.sent), 3)

    def test_connect_does_not_retry_device_errors(self) -> None:
        transport = ScriptedTransport(["ERR UNKNOWN_CMD PING"])
        ctl = pwm.PwmController(transport, reset_wait_s=0.0, ping_attempts=3,
                                sleep=lambda seconds: None)
        with self.assertRaises(pwm.PwmCommandError):
            ctl.connect()
        self.assertEqual(len(transport.sent), 1)

    def test_close_closes_transport(self) -> None:
        transport = ScriptedTransport(["OK PONG"])
        ctl = make_controller(transport)
        ctl.connect()
        ctl.close()
        self.assertTrue(transport.closed)
        self.assertFalse(ctl.connected)


class RampTest(unittest.TestCase):
    def test_ramp_up_sequence(self) -> None:
        lines = ["OK PCT {}".format(v) for v in (0, 10, 20, 30, 40)]
        transport = ScriptedTransport(["OK PONG"] + lines)
        sleeps = []  # type: List[float]
        ctl = pwm.PwmController(transport, reset_wait_s=0.0, sleep=sleeps.append)
        ctl.connect()
        final = ctl.ramp_speed_pct(0, 40, step_pct=10, step_interval_s=0.5)
        self.assertEqual(final, 40)
        self.assertEqual(transport.sent[1:], ["SET_PCT 0", "SET_PCT 10", "SET_PCT 20",
                                              "SET_PCT 30", "SET_PCT 40"])
        self.assertEqual(sleeps, [0.5, 0.5, 0.5, 0.5])

    def test_ramp_down_clamps_to_target(self) -> None:
        points = (70, 60, 53)
        transport = ScriptedTransport(["OK PONG"] + ["OK PCT {}".format(v) for v in points])
        ctl = make_controller(transport)
        ctl.connect()
        final = ctl.ramp_speed_pct(70, 53, step_pct=10, step_interval_s=0.0)
        self.assertEqual(final, 53)
        self.assertEqual(transport.sent[1:], ["SET_PCT 70", "SET_PCT 60", "SET_PCT 53"])

    def test_ramp_up_ends_exactly_on_target(self) -> None:
        transport = ScriptedTransport(["OK PONG"] + ["OK PCT {}".format(v)
                                                      for v in (0, 7, 14, 21, 25)])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertEqual(ctl.ramp_speed_pct(0, 25, step_pct=7, step_interval_s=0.0), 25)
        self.assertEqual(transport.sent[-1], "SET_PCT 25")

    def test_ramp_same_start_and_target_sends_once(self) -> None:
        transport = ScriptedTransport(["OK PONG", "OK PCT 30"])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertEqual(ctl.ramp_speed_pct(30, 30, step_pct=5), 30)
        self.assertEqual(transport.sent[1:], ["SET_PCT 30"])

    def test_ramp_to_zero_is_a_controlled_stop(self) -> None:
        transport = ScriptedTransport(["OK PONG"] + ["OK PCT {}".format(v) for v in (40, 20, 0)])
        ctl = make_controller(transport)
        ctl.connect()
        self.assertEqual(ctl.ramp_speed_pct(40, 0, step_pct=20, step_interval_s=0.0), 0)
        self.assertEqual(transport.sent[-1], "SET_PCT 0")

    def test_on_step_callback(self) -> None:
        transport = ScriptedTransport(["OK PONG"] + ["OK PCT {}".format(v) for v in (0, 50, 100)])
        ctl = make_controller(transport)
        ctl.connect()
        seen = []  # type: List[tuple]
        ctl.ramp_speed_pct(0, 100, step_pct=50, step_interval_s=0.0,
                           on_step=lambda requested, acked: seen.append((requested, acked)))
        self.assertEqual(seen, [(0, 0), (50, 50), (100, 100)])


class SimulatedDeviceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.device = pwm.SimulatedTransport()
        self.ctl = make_controller(self.device)
        self.ctl.connect()

    def test_boot_line_and_ping(self) -> None:
        self.assertTrue(self.ctl.ping())
        self.assertEqual(self.ctl.events[0], "BOOT pwm=0 ready=1")

    def test_starts_stopped(self) -> None:
        status = self.ctl.status()
        self.assertEqual(status.pwm, 0)
        self.assertEqual(status.en, 0)

    def test_set_pct_maps_to_pwm(self) -> None:
        self.assertEqual(self.ctl.set_speed_pct(60), 60)
        status = self.ctl.status()
        self.assertEqual(status.pwm, 153)
        self.assertEqual(status.pct, 60)
        self.assertEqual(status.target_pwm, 153)
        self.assertEqual(status.en, 1)

    def test_set_pwm_reports_percent(self) -> None:
        self.assertEqual(self.ctl.set_pwm(255), 255)
        self.assertEqual(self.ctl.status().pct, 100)
        self.assertEqual(self.ctl.set_pwm(0), 0)
        self.assertEqual(self.ctl.status().pct, 0)

    def test_stop_zeroes_output(self) -> None:
        self.ctl.set_speed_pct(80)
        self.assertEqual(self.ctl.stop(), "OK STOPPED")
        status = self.ctl.status()
        self.assertEqual((status.pwm, status.target_pwm, status.en), (0, 0, 0))
        self.assertEqual(self.device.stop_count, 1)

    def test_stop_is_idempotent_on_device(self) -> None:
        self.ctl.stop()
        self.ctl.stop()
        self.assertEqual(self.device.stop_count, 2)

    def test_device_rejects_out_of_range(self) -> None:
        self.device.send_line("SET_PCT 101")
        self.assertEqual(self.device.read_line(0.1), "ERR RANGE SET_PCT 0..100")
        self.device.send_line("SET_PWM 300")
        self.assertEqual(self.device.read_line(0.1), "ERR RANGE SET_PWM 0..255")

    def test_device_rejects_bad_arguments(self) -> None:
        for command in ("SET_PCT", "SET_PWM", "SET_PCT 6 0", "SET_PWM x"):
            self.device.send_line(command)
            self.assertTrue(str(self.device.read_line(0.1)).startswith("ERR "), command)

    def test_device_unknown_command(self) -> None:
        self.device.send_line("SPIN")
        self.assertEqual(self.device.read_line(0.1), "ERR UNKNOWN_CMD SPIN")

    def test_device_lowercase_and_heartbeat(self) -> None:
        self.device.send_line("ping")
        self.assertEqual(self.device.read_line(0.1), "OK PONG")
        self.device.send_line("heartbeat")
        self.assertEqual(self.device.read_line(0.1), "OK HEARTBEAT")

    def test_ramp_against_simulated_device(self) -> None:
        final = self.ctl.ramp_speed_pct(0, 70, step_pct=10, step_interval_s=0.0)
        self.assertEqual(final, 70)
        self.assertEqual(len(self.device.sent), 9)  # PING + SET_PCT 0,10,...,70
        self.assertEqual(self.ctl.status().pct, 70)

    def test_injected_event_surfaces(self) -> None:
        self.device.inject("EVT WATCHDOG_STOP pwm=0")
        self.ctl.ping()
        self.assertIn("EVT WATCHDOG_STOP pwm=0", self.ctl.events)


class StopActuatorTest(unittest.TestCase):
    def test_is_stop_actuator(self) -> None:
        device = pwm.SimulatedTransport()
        ctl = make_controller(device)
        ctl.connect()
        self.assertIsInstance(pwm.PwmStopActuator(ctl), StopActuator)

    def test_sends_stop_exactly_once(self) -> None:
        device = pwm.SimulatedTransport()
        ctl = make_controller(device)
        ctl.connect()
        ctl.set_speed_pct(60)
        actuator = pwm.PwmStopActuator(ctl, clock=lambda: 42.0)
        first = actuator.request_stop()
        self.assertEqual(first.outcome, "success")
        self.assertEqual(first.write_calls, 1)
        self.assertEqual(first.monotonic_s, 42.0)
        self.assertTrue(first.responded_at_utc.endswith("+00:00"))
        second = actuator.request_stop()
        self.assertEqual(second.outcome, "already_issued")
        self.assertEqual(second.write_calls, 0)
        self.assertEqual(device.stop_count, 1)
        self.assertEqual(device.pwm, 0)

    def test_send_failure_reports_connection_error_without_frame(self) -> None:
        transport = ScriptedTransport(["OK PONG"], fail_send=False)
        ctl = make_controller(transport)
        ctl.connect()
        transport.fail_send = True
        result = pwm.PwmStopActuator(ctl).request_stop()
        self.assertEqual(result.outcome, "connection_error")
        self.assertEqual(result.write_calls, 0)

    def test_no_reply_reports_timeout_after_one_frame(self) -> None:
        transport = ScriptedTransport(["OK PONG"])
        ctl = make_controller(transport, command_timeout_s=0.05)
        ctl.connect()
        result = pwm.PwmStopActuator(ctl).request_stop()
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(result.write_calls, 1)
        self.assertEqual(transport.sent[-1], "STOP")

    def test_device_error_reports_error_outcome(self) -> None:
        transport = ScriptedTransport(["OK PONG", "ERR UNKNOWN_CMD STOP"])
        ctl = make_controller(transport)
        ctl.connect()
        result = pwm.PwmStopActuator(ctl).request_stop()
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.write_calls, 1)


class ConfigCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(EXAMPLE_CONFIG, "r", encoding="utf-8") as handle:
            self.cfg = json.load(handle)

    def test_example_config_still_valid(self) -> None:
        from inductrl import config as config_mod
        self.assertEqual(config_mod.validate_config(self.cfg), [])

    def test_example_has_disabled_arduino_section(self) -> None:
        block = self.cfg["arduino_pwm"]
        self.assertFalse(block["enabled"])
        self.assertIsNone(block["port"])
        self.assertEqual(block["baud"], 115200)
        # 기존 최상위 항목은 그대로여야 합니다.
        for key in ("database", "host", "port", "unit_id", "channels", "decision",
                    "actuator", "stop_feedback", "function_code"):
            self.assertIn(key, self.cfg)

    def test_section_is_optional(self) -> None:
        from inductrl import config as config_mod
        cfg = dict(self.cfg)
        cfg.pop("arduino_pwm")
        cfg.pop("_arduino_pwm_note")
        self.assertEqual(config_mod.validate_config(cfg), [])

    def test_enabled_without_port_is_error(self) -> None:
        from inductrl import config as config_mod
        cfg = json.loads(json.dumps(self.cfg))
        cfg["arduino_pwm"]["enabled"] = True
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("arduino_pwm.enabled=true" in e for e in errors), errors)

    def test_enabled_with_port_passes(self) -> None:
        from inductrl import config as config_mod
        cfg = json.loads(json.dumps(self.cfg))
        cfg["arduino_pwm"]["enabled"] = True
        cfg["arduino_pwm"]["port"] = "/dev/ttyACM0"
        self.assertEqual(config_mod.validate_config(cfg), [])

    def test_bad_values_are_reported(self) -> None:
        from inductrl import config as config_mod
        cases = [
            ({"baud": 0}, "arduino_pwm.baud"),
            ({"baud": 115200.5}, "arduino_pwm.baud"),
            ({"port": ""}, "arduino_pwm.port"),
            ({"port": 3}, "arduino_pwm.port"),
            ({"ramp_step_pct": 0}, "arduino_pwm.ramp_step_pct"),
            ({"ramp_step_pct": 2.5}, "arduino_pwm.ramp_step_pct"),
            ({"ramp_interval_s": -1}, "arduino_pwm.ramp_interval_s"),
            ({"command_timeout_s": 0}, "arduino_pwm.command_timeout_s"),
            ({"enabled": "yes"}, "arduino_pwm.enabled"),
        ]
        for patch, needle in cases:
            cfg = json.loads(json.dumps(self.cfg))
            cfg["arduino_pwm"].update(patch)
            errors = config_mod.validate_config(cfg)
            self.assertTrue(any(needle in e for e in errors), (patch, errors))

    def test_config_section_defaults_and_merge(self) -> None:
        merged = pwm.config_section({"arduino_pwm": {"port": "/dev/ttyUSB0", "baud": 57600}})
        self.assertEqual(merged["port"], "/dev/ttyUSB0")
        self.assertEqual(merged["baud"], 57600)
        self.assertFalse(merged["enabled"])
        defaults = pwm.config_section(None)
        self.assertEqual(defaults["baud"], 115200)
        self.assertIsNone(defaults["port"])

    def test_controller_from_config_uses_port(self) -> None:
        cfg = {"arduino_pwm": {"enabled": False, "port": "/dev/ttyACM0", "baud": 57600}}
        controller = pwm.controller_from_config(cfg)
        self.assertIsInstance(controller.transport, pwm.PyserialTransport)
        self.assertEqual(controller.transport.port, "/dev/ttyACM0")
        self.assertEqual(controller.transport.baud, 57600)

    def test_controller_from_config_simulate(self) -> None:
        controller = pwm.controller_from_config({"arduino_pwm": {"port": None}}, simulate=True)
        self.assertIsInstance(controller.transport, pwm.SimulatedTransport)
        self.assertEqual(controller.reset_wait_s, 0.0)

    def test_controller_without_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            pwm.controller_from_config({"arduino_pwm": {"enabled": False, "port": None}})


@unittest.skipIf(HAS_PYSERIAL, "pyserial 이 설치된 환경에서는 설치 안내를 확인할 수 없습니다")
class PyserialMissingTest(unittest.TestCase):
    def test_open_without_pyserial_is_explained(self) -> None:
        transport = pwm.PyserialTransport("/dev/ttyACM0")
        with self.assertRaises(pwm.PwmTransportError) as ctx:
            transport.open()
        self.assertIn("pyserial", str(ctx.exception))

    def test_bad_port_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pwm.PyserialTransport("")


class CliTest(unittest.TestCase):
    def run_cli(self, argv: List[str]) -> "tuple[int, str, str]":
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pwm.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ping(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--ping", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["action"], "ping")
        self.assertEqual(payload["ping"], "PONG")

    def test_set_percent(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--pct", "60", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["pct"], 60)
        self.assertEqual(payload["status"]["pwm"], 153)
        self.assertEqual(payload["status"]["pct"], 60)

    def test_set_pwm(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--pwm", "128", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"]["pwm"], 128)

    def test_ramp(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--ramp", "0", "50", "--step", "25",
                                     "--interval", "0", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["pct"], 50)

    def test_stop(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--stop", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["stop"], "OK STOPPED")
        self.assertEqual(payload["status"]["pwm"], 0)

    def test_default_action_is_status(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"]["pwm"], 0)

    def test_human_output(self) -> None:
        code, out, _ = self.run_cli(["--simulate", "--pct", "60"])
        self.assertEqual(code, 0)
        self.assertIn("60%", out)
        self.assertIn("pwm=153", out)

    def test_invalid_values_exit_2_without_opening_port(self) -> None:
        for argv in (["--simulate", "--pct", "101"], ["--simulate", "--pwm", "-1"],
                     ["--simulate", "--ramp", "0", "100", "--step", "0"]):
            code, out, err = self.run_cli(argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.strip(), argv)

    def test_missing_port_exits_2(self) -> None:
        code, _, err = self.run_cli(["--pct", "10"])
        self.assertEqual(code, 2)
        self.assertIn("포트", err)

    def test_config_file_port_used(self) -> None:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump({"arduino_pwm": {"enabled": False, "port": None, "baud": 115200}}, handle)
            path = handle.name
        try:
            code, _, err = self.run_cli(["--config", path, "--status"])
            self.assertEqual(code, 2)
            self.assertIn("포트", err)
        finally:
            os.unlink(path)

    @unittest.skipIf(HAS_PYSERIAL, "pyserial 이 설치된 환경에서는 장치 열기 오류가 다릅니다")
    def test_transport_failure_exit_3(self) -> None:
        code, _, err = self.run_cli(["--port", "/dev/ttyACM_does_not_exist", "--ping"])
        self.assertEqual(code, 3)
        self.assertIn("통신 오류", err)


class StdlibOnlyTest(unittest.TestCase):
    def test_module_imports_without_pyserial(self) -> None:
        self.assertTrue(hasattr(pwm, "PwmController"))
        self.assertTrue(hasattr(pwm, "PwmStopActuator"))
        self.assertTrue(hasattr(pwm, "SerialTransport"))

    def test_module_file_has_no_write_helper_calls(self) -> None:
        """기존 안전 테스트와 같은 규칙: pwm 모듈은 Modbus 쓰기 이름을 쓰지 않습니다."""
        import re
        path = os.path.join(ROOT, "inductrl", "pwm_serial.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIsNone(re.search(r"\.write_[a-z_]*\s*\(", text))
        self.assertIsNone(re.search(r"def\s+(start_motor|restart|release|reset_do|clear_stop)\b", text))


if __name__ == "__main__":
    unittest.main()
