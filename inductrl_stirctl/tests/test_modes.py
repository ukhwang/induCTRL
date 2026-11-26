"""observe / live 경로, 액추에이터, live 시작 조건 시험 (가짜 Modbus 클라이언트 사용)."""
import io
import json
import os
import unittest
from unittest import mock

from inductrl import actuator as actuator_mod
from inductrl import cli
from inductrl import config as config_mod
from inductrl.modbus_io import ReadOnlyModbusClient, WriteBlockedError
from inductrl.models import (EV_CANDIDATE, EV_MODBUS_RESPONSE, EV_STOP_REQUESTED,
                             EV_STOP_UNCONFIRMED, EV_WARNING, FS_AUTO_STOP_CONFIRMED,
                             FS_CANDIDATE_RECORDED, FS_NO_CANDIDATE, FS_STOP_UNCONFIRMED)
from inductrl.runner import BatchRunner, LiveGateError, RunOptions
from inductrl.timeutil import SimClock

from tests.helpers import (FakeReadClient, FakeWriteClient, TempDirCase, example_cfg, make_plant,
                           verified_cfg)


class ReadOnlyWrapperTest(unittest.TestCase):
    def test_write_methods_raise(self) -> None:
        inner = mock.MagicMock()
        ro = ReadOnlyModbusClient(inner)
        for name in ("write_coil", "write_coils", "write_register", "write_registers",
                     "readwrite_registers", "mask_write_register", "execute"):
            with self.assertRaises(WriteBlockedError, msg=name):
                getattr(ro, name)
        self.assertEqual(inner.method_calls, [])
        ro.read_input_registers(3, count=1, slave=1)
        inner.read_input_registers.assert_called_once_with(3, count=1, slave=1)


class ObserveModeTest(TempDirCase):
    def _run(self, cfg, material: str = "saltwater", samples: int = 160):
        sim = SimClock()
        plant = make_plant(cfg, material, sim)
        fake = FakeReadClient(plant, cfg, sim.now)
        opts = RunOptions(mode="observe", material=material, batch_id="obs", samples=samples,
                          db_path=self.db_path, status_every=0)
        with mock.patch("inductrl.modbus_io.create_tcp_client", return_value=fake), \
                mock.patch("inductrl.actuator.create_write_client",
                           side_effect=AssertionError("observe 에서 쓰기 클라이언트 생성")) as wc:
            summary = BatchRunner(cfg, opts, sim.now, sim.sleep, io.StringIO(), io.StringIO()).run()
        wc.assert_not_called()
        return summary, fake

    def test_observe_records_candidate_with_zero_writes(self) -> None:
        summary, fake = self._run(verified_cfg())
        self.assertEqual(fake.write_calls, 0)
        self.assertGreater(fake.read_calls, 400)
        types = self.event_types("obs")
        self.assertIn(EV_CANDIDATE, types)
        self.assertNotIn(EV_STOP_REQUESTED, types)
        self.assertNotIn(EV_MODBUS_RESPONSE, types)
        self.assertEqual(summary.final_status, FS_CANDIDATE_RECORDED)
        self.assertTrue(self.first_event("obs", EV_CANDIDATE)["detail"]["verified"])

    def test_observe_unmapped_does_not_judge(self) -> None:
        cfg = verified_cfg()
        cfg["channels"]["conductivity"]["mapping_verified"] = False
        summary, fake = self._run(cfg, samples=120)
        self.assertEqual(fake.write_calls, 0)
        warn = self.first_event("obs", EV_WARNING)
        self.assertEqual(warn["detail"]["channel"], "conductivity")
        n = self.rows("SELECT count(*) FROM readings WHERE batch_id='obs' AND channel='conductivity' "
                      "AND status='UNMAPPED' AND value IS NULL")[0][0]
        self.assertEqual(n, 120)
        self.assertNotIn(EV_CANDIDATE, self.event_types("obs"))
        self.assertEqual(summary.final_status, FS_NO_CANDIDATE)


class LiveModeTest(TempDirCase):
    def _run_live(self, behavior: str, samples: int = 200):
        cfg = verified_cfg()
        sim = SimClock()
        plant = make_plant(cfg, "saltwater", sim)
        reader = FakeReadClient(plant, cfg, sim.now)
        writer = FakeWriteClient(behavior, plant, sim.now)
        opts = RunOptions(mode="live", material="saltwater", batch_id="live-" + behavior,
                          arm_stop=True, samples=samples, db_path=self.db_path, status_every=0)
        with mock.patch("inductrl.modbus_io.create_tcp_client", return_value=reader), \
                mock.patch("inductrl.actuator.create_write_client", return_value=writer) as wc:
            summary = BatchRunner(cfg, opts, sim.now, sim.sleep, io.StringIO(), io.StringIO()).run()
        wc.assert_called_once_with(cfg["host"], cfg["port"], cfg["write_timeout_s"])
        return summary, reader, writer, cfg

    def test_live_success_writes_once_and_confirms(self) -> None:
        summary, reader, writer, cfg = self._run_live("success")
        self.assertEqual(writer.write_calls, [(cfg["actuator"]["stop_coil_address"],
                                               cfg["actuator"]["stop_value"], cfg["unit_id"])])
        self.assertEqual(reader.write_calls, 0)
        self.assertEqual(summary.final_status, FS_AUTO_STOP_CONFIRMED)
        self.assertTrue(writer.closed)  # 종료 시 소켓만 닫음 (추가 쓰기 없음)

    def test_live_exception_response_is_unconfirmed(self) -> None:
        summary, reader, writer, _ = self._run_live("exception")
        self.assertEqual(len(writer.write_calls), 1)
        self.assertEqual(summary.final_status, FS_STOP_UNCONFIRMED)
        self.assertEqual(self.first_event("live-exception", EV_STOP_UNCONFIRMED)["detail"]["reason"],
                         "write_exception_response")

    def test_live_timeout_is_unconfirmed(self) -> None:
        summary, reader, writer, _ = self._run_live("io_return")
        self.assertEqual(len(writer.write_calls), 1)
        self.assertEqual(summary.final_status, FS_STOP_UNCONFIRMED)
        self.assertEqual(self.first_event("live-io_return", EV_STOP_UNCONFIRMED)["detail"]["reason"],
                         "write_timeout")


class ActuatorTest(unittest.TestCase):
    def _act(self, behavior: str):
        cfg = verified_cfg()
        writer = FakeWriteClient(behavior)
        with mock.patch("inductrl.actuator.create_write_client", return_value=writer):
            act = actuator_mod.ModbusStopActuator(cfg, lambda: 1.0)
        return act, writer

    def test_outcomes(self) -> None:
        expected = {"success": "success", "exception": "exception_response", "io_return": "timeout",
                    "raise_io": "timeout", "raise_conn": "connection_error",
                    "echo_mismatch": "echo_mismatch", "connect_fail": "connection_error"}
        for behavior, outcome in expected.items():
            act, writer = self._act(behavior)
            res = act.request_stop()
            self.assertEqual(res.outcome, outcome, behavior)
            self.assertEqual(len(writer.write_calls), 0 if behavior == "connect_fail" else 1, behavior)

    def test_second_request_never_writes(self) -> None:
        for behavior in ("success", "raise_io", "connect_fail"):
            act, writer = self._act(behavior)
            act.request_stop()
            n = len(writer.write_calls)
            res = act.request_stop()
            self.assertEqual(res.outcome, "already_issued")
            self.assertEqual(len(writer.write_calls), n)

    def test_requires_confirmed_stop_value(self) -> None:
        cfg = verified_cfg()
        cfg["actuator"]["stop_value"] = None
        with mock.patch("inductrl.actuator.create_write_client") as wc:
            with self.assertRaises(ValueError):
                actuator_mod.ModbusStopActuator(cfg, lambda: 0.0)
        wc.assert_not_called()

    def test_real_write_client_has_no_retries(self) -> None:
        try:
            import pymodbus  # noqa: F401
        except ImportError:
            self.skipTest("pymodbus 미설치")
        client = actuator_mod.create_write_client("127.0.0.1", 15999, 0.5)  # 연결하지 않음
        self.assertEqual(client.transaction.retries, 0)
        self.assertFalse(client.transaction.retry_on_empty)
        self.assertFalse(client.transaction.retry_on_invalid)
        self.assertIsNone(client.socket)

    def test_real_pymodbus_silent_server_sends_one_frame(self) -> None:
        """실제 pymodbus 로, 응답하지 않는 로컬 소켓에 정지 쓰기 → 프레임 1개만 전송, 결과는 timeout."""
        try:
            import pymodbus  # noqa: F401
        except ImportError:
            self.skipTest("pymodbus 미설치")
        import socket
        import threading
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        received = []
        accepted = []

        def serve() -> None:
            srv.settimeout(3.0)
            try:
                while True:
                    conn, _ = srv.accept()
                    accepted.append(conn)
                    conn.settimeout(1.5)
                    try:
                        while True:
                            data = conn.recv(256)
                            if not data:
                                break
                            received.append(data)
                    except OSError:
                        pass
            except OSError:
                pass

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        cfg = verified_cfg()
        cfg["host"], cfg["port"], cfg["write_timeout_s"] = "127.0.0.1", port, 0.3
        act = actuator_mod.ModbusStopActuator(cfg, lambda: 0.0)
        res = act.request_stop()
        again = act.request_stop()
        act.close()
        th.join(timeout=5.0)
        srv.close()
        for c in accepted:
            c.close()
        self.assertEqual(res.outcome, "timeout")
        self.assertEqual(again.outcome, "already_issued")
        frames = b"".join(received)
        self.assertEqual(len(frames), 12, frames)       # MBAP 7 + PDU 5 = Write Single Coil 1건
        self.assertEqual(frames[7], 0x05)               # 함수 코드 05
        self.assertEqual(len(accepted), 1)


class LiveGateTest(TempDirCase):
    def _missing(self, cfg, arm_stop: bool = True):
        return config_mod.check_live_readiness(cfg, "saltwater", arm_stop)

    def test_fully_verified_passes(self) -> None:
        self.assertEqual(self._missing(verified_cfg()), [])

    def _assert_refused(self, cfg, arm_stop: bool, needle: str) -> None:
        missing = self._missing(cfg, arm_stop)
        self.assertTrue(any(needle in m for m in missing), (needle, missing))
        opts = RunOptions(mode="live", material="saltwater", batch_id="g", arm_stop=arm_stop,
                          db_path=self.db_path, status_every=0)
        with mock.patch("inductrl.modbus_io.create_tcp_client") as rc, \
                mock.patch("inductrl.actuator.create_write_client") as wc:
            with self.assertRaises(LiveGateError):
                BatchRunner(cfg, opts, out=io.StringIO(), err=io.StringIO())
            # CLI 종료코드
            path = os.path.join(self.tmp, "cfg.json")
            cfg2 = dict(cfg)
            cfg2["database"] = self.db_path
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg2, f)
            argv = ["run", "--mode", "live", "--config", path, "--batch-id", "g",
                    "--material", "saltwater"] + (["--arm-stop"] if arm_stop else [])
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                code = cli.main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn(needle, err.getvalue())
        rc.assert_not_called()
        wc.assert_not_called()

    def test_missing_arm_stop(self) -> None:
        self._assert_refused(verified_cfg(), False, "--arm-stop")

    def test_unverified_mapping(self) -> None:
        cfg = verified_cfg()
        cfg["channels"]["conductivity"]["mapping_verified"] = False
        self._assert_refused(cfg, True, "channels.conductivity.mapping_verified")

    def test_unverified_rotation_mapping(self) -> None:
        cfg = verified_cfg()
        cfg["channels"]["rotation_counter"]["mapping_verified"] = False
        self._assert_refused(cfg, True, "channels.rotation_counter.mapping_verified")

    def test_unverified_thresholds(self) -> None:
        cfg = verified_cfg()
        cfg["decision"]["thresholds_verified"] = False
        self._assert_refused(cfg, True, "thresholds_verified")

    def test_null_threshold(self) -> None:
        cfg = verified_cfg()
        cfg["decision"]["saltwater"]["conductivity_max"] = None
        self._assert_refused(cfg, True, "conductivity_max")

    def test_missing_stop_value(self) -> None:
        cfg = verified_cfg()
        cfg["actuator"]["stop_value"] = None
        self._assert_refused(cfg, True, "actuator.stop_value")

    def test_missing_stop_address(self) -> None:
        cfg = verified_cfg()
        cfg["actuator"]["stop_coil_address"] = None
        self._assert_refused(cfg, True, "actuator.stop_coil_address")

    def test_actuator_not_enabled(self) -> None:
        cfg = verified_cfg()
        cfg["actuator"]["live_enabled"] = False
        self._assert_refused(cfg, True, "actuator.live_enabled")
        cfg = verified_cfg()
        cfg["actuator"]["verified"] = False
        self._assert_refused(cfg, True, "actuator.verified")

    def test_example_config_refused(self) -> None:
        self._assert_refused(example_cfg(), True, "actuator.stop_value")


if __name__ == "__main__":
    unittest.main()
