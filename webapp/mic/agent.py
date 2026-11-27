#!/usr/bin/env python3
"""Neon 명령 큐를 지켜보다가 실제 교반을 시작·정지하는 현장 에이전트.

전시장 구성
    목업 폰 → Vercel(/api/command) → Neon stir_monitor.stir_commands
    이 프로그램(MIC) → 1초마다 PENDING 명령 확인 → stirctl + 아두이노 PWM

하는 일
  · START: 아두이노 PWM 을 목표 세기로 올리고 `stirctl.py run` 을 띄웁니다.
  · STOP : PWM 을 즉시 0 으로 내리고 stirctl 프로세스에 SIGINT 를 보냅니다.
  · 교반이 끝나면(자동 정지 포함) PWM 을 0 으로 내리고 명령을 COMPLETED 로 마감합니다.
           (stirctl 은 Modbus 정지 코일이 없으면 모터를 직접 세우지 못하므로 여기서 세웁니다.)
  · 5초마다 생존 신호(mic_devices)를 남겨 폰 화면에 MIC 온라인/오프라인을 표시합니다.
  · 에이전트가 죽거나 SIGTERM 을 받으면 모터를 먼저 세웁니다.

실행 예 (현장)
    export NEON_UPLOAD_URL='postgresql://...'   # MIC 업로드 계정과 같은 값
    export MIC_SOURCE_ID='mic-01'
    python3 webapp/mic/agent.py --db /opt/stirctl/data/inductrl_readings.sqlite3 \
        --config /opt/stirctl/config.json --mode observe --pwm-port /dev/ttyACM0 --with-upload \
        --upload-state /var/lib/stir-upload/checkpoint.json

리허설(하드웨어 없이)
    python3 webapp/mic/agent.py --db /tmp/rehearsal.sqlite3 --mode mock --pwm-simulate

`NeonQueue`·`StirRunner`·`PwmDriver` 는 서로 독립적이라 테스트에서 갈아 끼울 수 있습니다.
psycopg2 와 pyserial 은 실제로 쓸 때만 불러옵니다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger("stir-agent")

AGENT_VERSION = "1.0"
DEFAULT_SOURCE_ID = "mic-01"
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 5.0
STOP_GRACE_SECONDS = 8.0
MAX_COMMAND_AGE_S = 120.0


# --------------------------------------------------------------------------- #
# stirctl 인자
# --------------------------------------------------------------------------- #
def new_batch_id(material: str, now: Optional[time.struct_time] = None) -> str:
    stamp = time.strftime("%m%d-%H%M%S", now or time.localtime())
    return "live-{}-{}-{:04x}".format(material, stamp, random.randint(0, 0xFFFF))


def build_stirctl_args(*, mode: str, material: str, batch_id: str, db: str,
                       config: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
                       arm_stop: bool = False, mock_speed: float = 5.0,
                       status_every: int = 0) -> List[str]:
    """stirctl.py run 인자를 만듭니다. mock 전용 인자는 mock 일 때만 붙입니다."""
    args = ["stirctl.py", "run", "--mode", mode, "--material", material,
            "--batch-id", batch_id, "--db", db, "--status-every", str(status_every)]
    if config:
        args += ["--config", config]
    if mode == "live" and arm_stop:
        args.append("--arm-stop")
    if mode == "mock":
        payload = payload or {}
        control = payload.get("control_mode", "rule")
        args += ["--water-l", str(payload.get("water_l", 10)),
                 "--solute-g", str(payload.get("solute_g", 500)),
                 "--control-mode", str(control),
                 "--speed-factor", str(mock_speed)]
    return args


# --------------------------------------------------------------------------- #
# Neon 명령 큐
# --------------------------------------------------------------------------- #
CLAIM_SQL = """
UPDATE stir_monitor.stir_commands AS c
   SET status = 'RUNNING', updated_at = now(), started_at = now()
 WHERE c.id = (
     SELECT id FROM stir_monitor.stir_commands
      WHERE mic_source_id = %s AND status = 'PENDING'
      ORDER BY (action <> 'STOP'), id
      FOR UPDATE SKIP LOCKED
      LIMIT 1)
 RETURNING c.id, c.action, c.payload, c.created_at
"""

FINISH_SQL = """
UPDATE stir_monitor.stir_commands
   SET status = %s, updated_at = now(), finished_at = now(),
       error_message = %s, result = %s::jsonb
 WHERE id = %s AND mic_source_id = %s
"""

HEARTBEAT_SQL = """
INSERT INTO stir_monitor.mic_devices
    (mic_source_id, last_seen_at, agent_state, stirctl_mode, active_batch_id,
     current_command_id, agent_pid, agent_version, detail_json)
VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (mic_source_id) DO UPDATE SET
    last_seen_at = now(),
    agent_state = EXCLUDED.agent_state,
    stirctl_mode = EXCLUDED.stirctl_mode,
    active_batch_id = EXCLUDED.active_batch_id,
    current_command_id = EXCLUDED.current_command_id,
    agent_pid = EXCLUDED.agent_pid,
    agent_version = EXCLUDED.agent_version,
    detail_json = EXCLUDED.detail_json
"""


class NeonQueue:
    """stir_commands / mic_devices 접근. 연결이 끊기면 다음 호출에서 다시 붙습니다."""

    def __init__(self, dsn: str, source: str, connect: Optional[Callable[[], Any]] = None) -> None:
        self.dsn = dsn
        self.source = source
        self._connect = connect
        self._conn = None  # type: Any

    def _connection(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0):
            self._conn = None
        if self._conn is None:
            if self._connect is not None:
                conn = self._connect()
            else:
                import psycopg2  # 하드웨어/현장에서만 필요
                conn = psycopg2.connect(self.dsn, connect_timeout=4, application_name="stir-agent")
            conn.autocommit = True
            self._conn = conn
        return self._conn

    def _run(self, sql: str, params: tuple) -> List[tuple]:
        conn = self._connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall() if cursor.description else []
        except Exception:
            # 끊긴 연결은 버리고 다음 호출에서 새로 붙습니다.
            try:
                conn.close()
            except Exception:
                pass
            self._conn = None
            raise

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def claim(self) -> Optional[Dict[str, Any]]:
        rows = self._run(CLAIM_SQL, (self.source,))
        if not rows:
            return None
        command_id, action, payload, created_at = rows[0]
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        return {"id": int(command_id), "action": action, "payload": payload or {},
                "created_at": created_at}

    def finish(self, command_id: int, status: str, error: Optional[str] = None,
               result: Optional[Dict[str, Any]] = None) -> None:
        self._run(FINISH_SQL, (status, error, json.dumps(result or {}), command_id, self.source))

    def heartbeat(self, **fields: Any) -> None:
        detail = fields.get("detail") or {}
        self._run(HEARTBEAT_SQL, (
            self.source,
            fields.get("state", "idle"),
            fields.get("mode"),
            fields.get("batch_id"),
            fields.get("command_id"),
            os.getpid(),
            AGENT_VERSION,
            json.dumps(detail, ensure_ascii=False, default=str),
        ))

    def cancel_pending_starts(self, reason: str) -> int:
        rows = self._run(
            "UPDATE stir_monitor.stir_commands "
            "SET status = 'CANCELED', updated_at = now(), error_message = %s "
            "WHERE mic_source_id = %s AND action = 'START' AND status = 'PENDING' "
            "RETURNING id",
            (reason, self.source),
        )
        return len(rows)

    def recover(self, max_age_s: float = MAX_COMMAND_AGE_S) -> Dict[str, int]:
        """에이전트가 꺼져 있던 동안 쌓인 명령을 안전한 상태로 정리합니다."""
        failed = self._run(
            "UPDATE stir_monitor.stir_commands "
            "SET status = 'FAILED', updated_at = now(), "
            "    error_message = '에이전트가 재시작되어 이전 실행 상태를 확인할 수 없습니다.' "
            "WHERE mic_source_id = %s AND action = 'START' AND status = 'RUNNING' RETURNING id",
            (self.source,),
        )
        stale = self._run(
            "UPDATE stir_monitor.stir_commands "
            "SET status = 'CANCELED', updated_at = now(), "
            "    error_message = '오래된 시작 명령이라 실행하지 않았습니다.' "
            "WHERE mic_source_id = %s AND action = 'START' AND status = 'PENDING' "
            "AND created_at < now() - make_interval(secs => %s::double precision) RETURNING id",
            (self.source, max_age_s),
        )
        stops = self._run(
            "UPDATE stir_monitor.stir_commands "
            "SET status = 'COMPLETED', updated_at = now(), finished_at = now(), "
            "    error_message = '에이전트 재시작 전에 들어온 정지 명령입니다.' "
            "WHERE mic_source_id = %s AND action = 'STOP' AND status IN ('PENDING', 'RUNNING') "
            "AND created_at < now() - make_interval(secs => %s::double precision) RETURNING id",
            (self.source, max_age_s),
        )
        return {"failed": len(failed), "stale": len(stale), "stops": len(stops)}


# --------------------------------------------------------------------------- #
# stirctl 프로세스
# --------------------------------------------------------------------------- #
class StirRunner:
    """stirctl.py 자식 프로세스 하나를 관리합니다."""

    def __init__(self, *, python: str, cwd: str, db: str, mode: str, config: Optional[str],
                 arm_stop: bool = False, mock_speed: float = 5.0,
                 spawn: Optional[Callable[..., Any]] = None, log: Callable[[str], None] = LOGGER.info) -> None:
        self.python = python
        self.cwd = cwd
        self.db = db
        self.mode = mode
        self.config = config
        self.arm_stop = arm_stop
        self.mock_speed = mock_speed
        self._spawn = spawn or subprocess.Popen
        self._process = None  # type: Any
        self._last_exit = None  # type: Optional[Dict[str, Any]]
        self._log = log
        self.batch_id = None  # type: Optional[str]

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, *, material: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        batch_id = new_batch_id(material)
        args = build_stirctl_args(mode=self.mode, material=material, batch_id=batch_id, db=self.db,
                                  config=self.config, payload=payload, arm_stop=self.arm_stop,
                                  mock_speed=self.mock_speed)
        self._log("stirctl 시작: " + " ".join(args))
        try:
            self._process = self._spawn([self.python] + args, cwd=self.cwd, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as error:
            self._process = None
            return {"ok": False, "error": "spawn_failed", "message": str(error)}
        self.batch_id = batch_id
        self._last_exit = None
        return {"ok": True, "batch_id": batch_id, "argv": args}

    def poll(self) -> Optional[Dict[str, Any]]:
        if self._process is None:
            return None
        code = self._process.poll()
        if code is None:
            return None
        info = {"code": code, "signal": -code if code is not None and code < 0 else None}
        self._log("stirctl 종료: code={} signal={}".format(info["code"], info["signal"]))
        self._process = None
        self._last_exit = info
        return info

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> Dict[str, Any]:
        process = self._process
        if process is None:
            return {"stopped": False}
        try:
            process.send_signal(signal.SIGINT)
        except Exception:
            pass
        try:
            process.wait(timeout=grace)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        info = self.poll()
        return {"stopped": True, "exit": info}

    @property
    def last_exit(self) -> Optional[Dict[str, Any]]:
        return self._last_exit


# --------------------------------------------------------------------------- #
# 아두이노 PWM
# --------------------------------------------------------------------------- #
class PwmDriver:
    """아두이노 우노 PWM 모터 제어. --no-pwm 이면 아무 동작도 하지 않습니다."""

    def __init__(self, *, stirctl_dir: str, config: Optional[str] = None, port: Optional[str] = None,
                 simulate: bool = False, enabled: bool = True,
                 controller_factory: Optional[Callable[[], Any]] = None,
                 log: Callable[[str], None] = LOGGER.info) -> None:
        self.stirctl_dir = stirctl_dir
        self.config_path = config
        self.port = port
        self.simulate = simulate
        self.enabled = enabled
        self._factory = controller_factory
        self._controller = None  # type: Any
        self._log = log

    def _config(self) -> Dict[str, Any]:
        if not self.config_path:
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as error:
            self._log("PWM 설정을 읽지 못했습니다: {}".format(error))
            return {}

    def _ensure(self) -> Any:
        if self._controller is not None:
            return self._controller
        if not self.enabled:
            return None
        if self._factory is not None:
            self._controller = self._factory()
            return self._controller
        if self.stirctl_dir not in sys.path:
            sys.path.insert(0, self.stirctl_dir)
        from inductrl.pwm_serial import controller_from_config  # 현장에서만 필요
        self._controller = controller_from_config(self._config(), port=self.port, simulate=self.simulate)
        self._controller.connect()
        self._log("PWM 연결됨 (port={}, simulate={})".format(self.port, self.simulate))
        return self._controller

    def set_speed(self, pct: Any) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            value = max(0, min(100, int(round(float(pct)))))
        except (TypeError, ValueError):
            value = 60
        try:
            controller = self._ensure()
            if controller is None:
                return None
            applied = controller.set_speed_pct(value)
            self._log("PWM {}% 적용".format(applied if applied is not None else value))
            return value
        except Exception as error:  # 포트가 없어도 stirctl 관측은 계속합니다.
            self._log("PWM 속도 설정 실패: {}: {}".format(type(error).__name__, error))
            return None

    def stop(self) -> bool:
        """모터 정지. 실패해도 예외를 올리지 않습니다(정지 경로는 항상 끝까지 갑니다)."""
        if not self.enabled:
            return False
        try:
            controller = self._ensure()
            if controller is None:
                return False
            controller.stop()
            self._log("PWM 정지")
            return True
        except Exception as error:
            self._log("PWM 정지 실패: {}: {}".format(type(error).__name__, error))
            return False

    def close(self) -> None:
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                pass
            self._controller = None


# --------------------------------------------------------------------------- #
# 에이전트
# --------------------------------------------------------------------------- #
def batch_final_status(db_path: Optional[str], batch_id: Optional[str]) -> Optional[str]:
    if not db_path or not batch_id:
        return None
    try:
        with sqlite3.connect("file:{}?mode=ro".format(Path(db_path).as_posix()), uri=True, timeout=2) as con:
            row = con.execute("SELECT final_status FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


class Agent:
    """한 번에 하나의 교반만 실행합니다. tick() 한 번이 폴링 1회입니다."""

    def __init__(self, *, queue: Any, runner: Any, pwm: Any, source: str = DEFAULT_SOURCE_ID,
                 mode: str = "observe", db_path: Optional[str] = None,
                 poll_seconds: float = POLL_SECONDS, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = LOGGER.info) -> None:
        self.queue = queue
        self.runner = runner
        self.pwm = pwm
        self.source = source
        self.mode = mode
        self.db_path = db_path
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._sleep = sleep
        self._log = log
        self._stopping = False
        self.state = "idle"
        self.active_command = None  # type: Optional[Dict[str, Any]]
        self.batch_id = None  # type: Optional[str]
        self._last_heartbeat = 0.0
        self.last_error = None  # type: Optional[str]

    # ── 생명주기 ────────────────────────────────────────────────────────
    def start(self) -> None:
        self.pwm.stop()                      # 켜질 때 모터가 서 있는지 먼저 확인합니다.
        try:
            recovered = self.queue.recover()
            if any(recovered.values()):
                self._log("이전 명령 정리: {}".format(recovered))
        except Exception as error:
            self._log("이전 명령 정리 실패(계속 진행): {}: {}".format(type(error).__name__, error))
        self.heartbeat(force=True)

    def run(self) -> None:
        self.start()
        while not self._stopping:
            try:
                self.tick()
            except Exception as error:
                self.last_error = "{}: {}".format(type(error).__name__, error)
                self._log("폴링 오류: {}".format(self.last_error))
            self._sleep(self.poll_seconds)

    def request_stop(self, reason: str = "에이전트 종료") -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    def shutdown(self, reason: str = "에이전트 종료") -> None:
        self._stopping = True
        self.pwm.stop()
        self.runner.stop()
        self.state = "stopped"
        try:
            self.queue.cancel_pending_starts(reason)
            self.heartbeat(force=True)
        except Exception:
            pass
        self.pwm.close()
        self.queue.close()

    # ── 한 주기 ─────────────────────────────────────────────────────────
    def tick(self) -> Dict[str, Any]:
        if self._stopping:
            return {"claimed": None, "state": self.state}
        finished = self.runner.poll()
        if finished is not None:
            self._finish_active(finished)

        command = self.queue.claim()
        handled = None
        if command is not None:
            handled = command["action"]
            if command["action"] == "STOP":
                self._handle_stop(command)
            elif command["action"] == "START":
                self._handle_start(command)
            else:
                self.queue.finish(command["id"], "FAILED", "알 수 없는 명령입니다: {}".format(command["action"]))
        self.heartbeat()
        return {"claimed": handled, "state": self.state}

    def _handle_start(self, command: Dict[str, Any]) -> None:
        if self.runner.alive():
            self.queue.finish(command["id"], "FAILED", "이미 교반이 실행 중입니다.")
            self._log("중복 시작 거부: 이미 실행 중")
            return
        payload = command.get("payload") or {}
        material = payload.get("material")
        if material not in ("saltwater", "caco3"):
            self.queue.finish(command["id"], "FAILED", "재료 값이 올바르지 않습니다.")
            return
        self.active_command = command
        self.state = "starting"
        applied = self.pwm.set_speed(payload.get("target_rpm_pct", 60))
        if getattr(self.pwm, "enabled", False) and applied is None:
            # 모터를 못 올렸는데 '교반 중'으로 보이면 현장에서 혼란이 생깁니다.
            self.pwm.stop()
            self.state = "idle"
            self.active_command = None
            self.queue.finish(command["id"], "FAILED",
                              "아두이노 PWM 제어에 실패했습니다. 포트(--pwm-port)와 전원을 확인하세요.")
            self._log("PWM 실패로 시작 취소 (명령 {})".format(command["id"]))
            return
        started = self.runner.start(material=material, payload=payload)
        if not started.get("ok"):
            self.pwm.stop()
            self.state = "idle"
            self.active_command = None
            self.queue.finish(command["id"], "FAILED",
                              started.get("message") or "stirctl 을 실행하지 못했습니다.")
            return
        self.batch_id = started["batch_id"]
        self.state = "running"
        self._log("교반 시작: 명령 {} · 배치 {}".format(command["id"], self.batch_id))
        self.heartbeat(force=True)

    def _handle_stop(self, command: Dict[str, Any]) -> None:
        self.pwm.stop()                       # 모터부터 세웁니다.
        outcome = self.runner.stop()
        canceled = 0
        try:
            canceled = self.queue.cancel_pending_starts("정지 요청으로 취소")
        except Exception:
            pass
        if self.active_command is not None:
            self._finish_active({"code": (outcome.get("exit") or {}).get("code"), "signal": "SIGINT",
                                 "stopped": True}, motor_stopped=True)
        self.queue.finish(command["id"], "COMPLETED", None,
                          {"stopped": outcome.get("stopped", False), "canceled_starts": canceled})
        self.state = "idle"
        self._log("정지 완료 (명령 {})".format(command["id"]))

    def _finish_active(self, exit_info: Dict[str, Any], motor_stopped: bool = False) -> None:
        command = self.active_command
        if not motor_stopped:
            self.pwm.stop()                   # 자동 정지 뒤에도 모터가 돌지 않게 합니다.
        self.state = "idle"
        self.active_command = None
        batch_id = self.batch_id
        self.batch_id = None
        if command is None:
            return
        code = exit_info.get("code")
        failed = isinstance(code, int) and code not in (0, 130)
        status = "FAILED" if failed else "COMPLETED"
        error = None if not failed else "stirctl 종료 코드 {}".format(code)
        self.queue.finish(command["id"], status, error, {
            "batch_id": batch_id,
            "final_status": batch_final_status(self.db_path, batch_id),
            "exit_code": code,
            "signal": exit_info.get("signal"),
        })
        self._log("명령 {} 종료 → {} (배치 {})".format(command["id"], status, batch_id))

    # ── 생존 신호 ───────────────────────────────────────────────────────
    def heartbeat(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_heartbeat < self.heartbeat_seconds:
            return
        self._last_heartbeat = now
        try:
            self.queue.heartbeat(
                state=self.state,
                mode=self.mode,
                batch_id=self.batch_id,
                command_id=self.active_command["id"] if self.active_command else None,
                detail={"last_error": self.last_error, "runner": self.runner.last_exit},
            )
        except Exception as error:
            self._log("생존 신호 실패: {}: {}".format(type(error).__name__, error))


# --------------------------------------------------------------------------- #
# 업로더 동반 실행
# --------------------------------------------------------------------------- #
class UploaderChild:
    """upload.py 를 함께 띄워 두고, 죽으면 다시 살립니다."""

    def __init__(self, *, python: str, script: Path, db: str, state: Path,
                 spawn: Optional[Callable[..., Any]] = None,
                 log: Callable[[str], None] = LOGGER.info) -> None:
        self.python = python
        self.script = script
        self.db = db
        self.state = state
        self._spawn = spawn or subprocess.Popen
        self._process = None  # type: Any
        self._next_try = 0.0
        self._log = log

    def ensure(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self._process is not None and self._process.poll() is None:
            return
        if now < self._next_try:
            return
        self._process = None
        try:
            self._process = self._spawn(
                [self.python, str(self.script), "--db", self.db, "--state", str(self.state)],
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._log("업로더 시작")
        except OSError as error:
            self._next_try = now + 10.0
            self._log("업로더를 시작하지 못했습니다: {}".format(error))

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stir-agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="stirctl 이 쓰는 SQLite 경로")
    parser.add_argument("--config", default=None, help="stirctl 설정 파일(config.json)")
    parser.add_argument("--mode", choices=("observe", "live", "mock"), default="observe",
                        help="observe=센서 관측(기본), live=Modbus 정지 쓰기 허용, mock=하드웨어 없이 리허설")
    parser.add_argument("--arm-stop", action="store_true", help="live 모드에서 Modbus 정지 쓰기 허용(둘 다 있어야 동작)")
    parser.add_argument("--source", default=None, help="MIC_SOURCE_ID (기본: 환경변수 또는 mic-01)")
    parser.add_argument("--stirctl-dir", default=None, help="inductrl_stirctl 디렉터리 (기본: 저장소 상대 경로)")
    parser.add_argument("--python", default=sys.executable, help="stirctl 을 실행할 파이썬")
    parser.add_argument("--pwm-port", default=None, help="아두이노 시리얼 포트 (예: /dev/ttyACM0)")
    parser.add_argument("--pwm-simulate", action="store_true", help="아두이노 없이 PWM 대화를 흉내냅니다")
    parser.add_argument("--no-pwm", action="store_true", help="PWM 제어를 쓰지 않습니다(Modbus 정지 코일만 쓰는 경우)")
    parser.add_argument("--mock-speed", type=float, default=5.0, help="mock 모드 재생 배율")
    parser.add_argument("--with-upload", action="store_true", help="upload.py 를 함께 실행해 센서 값을 Neon 으로 올립니다")
    parser.add_argument("--upload-script", default=None, help="upload.py 경로 (기본: 이 파일 옆 upload.py)")
    parser.add_argument("--upload-state", default=None, help="업로더 체크포인트 경로 (--with-upload 필수)")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    parser.add_argument("--heartbeat", type=float, default=HEARTBEAT_SECONDS)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    dsn = os.environ.get("NEON_AGENT_URL") or os.environ.get("NEON_UPLOAD_URL")
    source = args.source or os.environ.get("MIC_SOURCE_ID") or DEFAULT_SOURCE_ID
    if not dsn:
        LOGGER.error("NEON_UPLOAD_URL(또는 NEON_AGENT_URL) 환경변수가 필요합니다.")
        return 2

    here = Path(__file__).resolve().parent
    stirctl_dir = Path(args.stirctl_dir) if args.stirctl_dir else here.parent.parent / "inductrl_stirctl"
    if not (stirctl_dir / "stirctl.py").exists():
        LOGGER.error("stirctl.py 를 찾지 못했습니다: %s", stirctl_dir)
        return 2

    queue = NeonQueue(dsn, source)
    runner = StirRunner(python=args.python, cwd=str(stirctl_dir), db=args.db, mode=args.mode,
                        config=args.config, arm_stop=args.arm_stop, mock_speed=args.mock_speed)
    pwm = PwmDriver(stirctl_dir=str(stirctl_dir), config=args.config, port=args.pwm_port,
                    simulate=args.pwm_simulate, enabled=not args.no_pwm)
    agent = Agent(queue=queue, runner=runner, pwm=pwm, source=source, mode=args.mode,
                  db_path=args.db, poll_seconds=args.poll, heartbeat_seconds=args.heartbeat)

    uploader = None
    if args.with_upload:
        if not args.upload_state:
            LOGGER.error("--with-upload 에는 --upload-state 가 필요합니다.")
            return 2
        uploader = UploaderChild(python=args.python,
                                 script=Path(args.upload_script) if args.upload_script else here / "upload.py",
                                 db=args.db, state=Path(args.upload_state))

    def handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("신호 %s — 모터를 세우고 종료합니다.", signum)
        agent.request_stop("신호 {}".format(signum))

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    LOGGER.info("에이전트 시작: source=%s mode=%s db=%s", source, args.mode, args.db)
    try:
        agent.start()
        while not agent.stopping:
            if uploader is not None:
                uploader.ensure()
            try:
                agent.tick()
            except Exception as error:
                agent.last_error = "{}: {}".format(type(error).__name__, error)
                LOGGER.warning("폴링 오류: %s", agent.last_error)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        if uploader is not None:
            uploader.close()
        agent.shutdown()
        LOGGER.info("에이전트 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
