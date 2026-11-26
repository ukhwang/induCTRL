"""stirctl 명령행.

  run         배치 실행 (mock / observe / live)
  initdb      SQLite 스키마만 만들기(웹앱 시뮬레이터 첫 실행용)
  sample add  시료 측정값 수동 입력
  quality set 품질 합격 여부
  manual-stop 운영자 수동 정지 시각
  batch show  배치 요약 / batch list
  replay      저장된 원시값으로 재계산
"""
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import db as db_mod
from . import manual
from . import sim as sim_mod
from .mock import SCENARIOS
from .models import MATERIALS, MODES, START_TRIGGERS
from .replay import ReplayError, format_report, replay_report
from .runner import BatchExistsError, LiveGateError, RunOptions, run_batch

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONFIG = "config.example.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stirctl", description="InduCTRL 교반 완료 판정·자동 정지")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="배치 실행")
    r.add_argument("--mode", choices=MODES, default="mock")
    r.add_argument("--config", default=DEFAULT_CONFIG)
    r.add_argument("--db", default=None, help="DB 경로(설정의 database 대신)")
    r.add_argument("--batch-id", required=True)
    r.add_argument("--material", choices=MATERIALS, required=True)
    r.add_argument("--start-trigger", choices=START_TRIGGERS, default="rpm")
    r.add_argument("--arm-stop", action="store_true", help="live 정지 쓰기 허용(필수 조건 중 하나)")
    r.add_argument("--samples", type=int, default=0, help="0 = Ctrl+C 까지 (mock --fast 는 240)")
    r.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal", help="mock 전용")
    r.add_argument("--fast", action="store_true", help="mock 전용: 가상 시계로 대기 없이 실행")
    r.add_argument("--status-every", type=int, default=10, help="N 주기마다 상태 줄 출력(0=끔)")
    # 실시간 시뮬레이터(모두 선택). 하나라도 주면 시뮬레이션 모드로 동작합니다.
    r.add_argument("--water-l", type=float, default=None, help="물 투입량(L), 예: 10")
    r.add_argument("--solute-g", type=float, default=None, help="용질 투입량(g), 예: 500")
    r.add_argument("--control-mode", choices=sim_mod.CONTROL_MODES, default=None,
                   help="fixed|rule|ai (시뮬레이션 기본 rule)")
    r.add_argument("--speed-factor", type=float, default=None,
                   help="실시간 배율(1x/5x/10x). 기본 1")
    r.add_argument("--fixed-seconds", type=float, default=None,
                   help="fixed 기준 운전 시간(초). 기본 {:.0f}".format(sim_mod.FIXED_SECONDS))

    i = sub.add_parser("initdb", help="SQLite 스키마만 준비(웹앱 첫 실행용)")
    i.add_argument("--db", required=True, help="만들거나 확인할 DB 경로")
    i.add_argument("--json", action="store_true")

    def add_db_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default=None, help="DB 경로를 읽을 설정 파일")
        sp.add_argument("--db", default=None, help="DB 경로")
        sp.add_argument("--json", action="store_true", help="JSON 출력")

    s = sub.add_parser("sample", help="시료 측정값")
    ssub = s.add_subparsers(dest="action")
    sa = ssub.add_parser("add")
    sa.add_argument("--batch-id", required=True)
    sa.add_argument("--position", choices=manual.POSITIONS, required=True)
    sa.add_argument("--metric", required=True, help="예: turbidity, conductivity, temperature, power")
    sa.add_argument("--value", type=float, required=True)
    sa.add_argument("--unit", default=None)
    sa.add_argument("--measured-at", default=None, help="ISO 8601 (예: 2026-12-01T14:03:05+09:00)")
    sa.add_argument("--note", default=None)
    add_db_args(sa)

    q = sub.add_parser("quality", help="품질 합격 여부")
    qsub = q.add_subparsers(dest="action")
    qs = qsub.add_parser("set")
    qs.add_argument("--batch-id", required=True)
    qs.add_argument("--passed", choices=("yes", "no"), required=True)
    qs.add_argument("--note", default=None)
    add_db_args(qs)

    m = sub.add_parser("manual-stop", help="운영자 수동 정지 시각 기록(쓰기 없음)")
    m.add_argument("--batch-id", required=True)
    m.add_argument("--at", default=None, help="ISO 8601. 생략하면 지금")
    m.add_argument("--note", default=None)
    add_db_args(m)

    b = sub.add_parser("batch", help="배치 조회")
    bsub = b.add_subparsers(dest="action")
    bs = bsub.add_parser("show")
    bs.add_argument("--batch-id", required=True)
    add_db_args(bs)
    bl = bsub.add_parser("list")
    add_db_args(bl)

    rp = sub.add_parser("replay", help="저장된 원시값으로 재계산")
    rp.add_argument("--batch-id", required=True)
    rp.add_argument("--config", default=None, help="비교할 다른 설정(주면 나란히 비교)")
    rp.add_argument("--db", default=None, help="DB 경로")
    rp.add_argument("--json", action="store_true")
    return p


def resolve_db_path(db: Optional[str], config_path: Optional[str]) -> str:
    if db:
        return db
    path = config_path or DEFAULT_CONFIG
    if os.path.exists(path):
        cfg = config_mod.load_config(path)
        if isinstance(cfg.get("database"), str) and cfg["database"]:
            return cfg["database"]
    return "data/inductrl_readings.sqlite3"


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_run(args: argparse.Namespace) -> int:
    if args.samples < 0:
        print("ERROR: --samples 는 0 이상", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = config_mod.load_config(args.config)
    except (OSError, ValueError) as exc:
        print("ERROR: 설정 파일을 읽을 수 없습니다: {}".format(exc), file=sys.stderr)
        return EXIT_CONFIG

    sim = _build_sim(args)
    if sim is not None and args.mode != "mock":
        print("ERROR: --water-l/--solute-g/--control-mode/--speed-factor 는 --mode mock 전용입니다",
              file=sys.stderr)
        return EXIT_CONFIG
    samples = args.samples
    stop_grace = None  # type: Optional[float]
    if sim is not None:
        problems = sim_mod.validate(sim)
        if problems:
            print("ERROR: " + "; ".join(problems), file=sys.stderr)
            return EXIT_CONFIG
        interval = float(cfg["decision"]["interval_seconds"])
        if samples == 0:
            cap = sim.fixed_seconds if sim.control_mode == "fixed" else 180.0
            samples = max(1, int(round(cap / interval)))
        stop_grace = None if sim.control_mode == "fixed" else 6.0

    opts = RunOptions(mode=args.mode, material=args.material, batch_id=args.batch_id,
                      start_trigger=args.start_trigger, arm_stop=args.arm_stop, samples=samples,
                      scenario=args.scenario, fast=args.fast, db_path=args.db,
                      status_every=args.status_every, sim=sim, stop_grace_s=stop_grace)
    try:
        summary = run_batch(cfg, opts)
    except LiveGateError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_CONFIG
    except (config_mod.ConfigError, BatchExistsError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n사용자 중단 — 정지 명령은 보내지 않았습니다.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        print("ERROR: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return EXIT_RUNTIME
    print("완료: batch={} 최종 상태={} 주기={}".format(summary.batch_id, summary.final_status, summary.cycles))
    return EXIT_OK


def _build_sim(args: argparse.Namespace) -> Optional[sim_mod.SimParams]:
    """시뮬레이션 인자가 하나라도 있으면 SimParams 를 만듭니다(없으면 기존 동작)."""
    given = (args.water_l, args.solute_g, args.control_mode, args.speed_factor, args.fixed_seconds)
    if all(value is None for value in given):
        return None
    defaults = sim_mod.SimParams()
    return sim_mod.SimParams(
        water_l=defaults.water_l if args.water_l is None else args.water_l,
        solute_g=defaults.solute_g if args.solute_g is None else args.solute_g,
        control_mode=defaults.control_mode if args.control_mode is None else args.control_mode,
        speed_factor=defaults.speed_factor if args.speed_factor is None else args.speed_factor,
        fixed_seconds=(defaults.fixed_seconds if args.fixed_seconds is None else args.fixed_seconds),
    )


def cmd_initdb(args: argparse.Namespace) -> int:
    con = db_mod.connect(args.db)
    con.close()
    if args.json:
        _print_json({"ok": True, "db": args.db})
    else:
        print("DB 준비 완료: {}".format(args.db))
    return EXIT_OK


def _open(args: argparse.Namespace) -> Any:
    return db_mod.connect(resolve_db_path(args.db, args.config))


def cmd_manual(args: argparse.Namespace) -> int:
    con = _open(args)
    try:
        if args.command == "sample":
            rid = manual.add_sample(con, args.batch_id, args.position, args.metric, args.value,
                                    args.unit, args.measured_at, args.note)
        elif args.command == "quality":
            rid = manual.set_quality(con, args.batch_id, args.passed == "yes", args.note)
        else:
            rid = manual.manual_stop(con, args.batch_id, args.at, args.note)
        print("저장됨: manual_entries.id={}".format(rid))
        return EXIT_OK
    except (manual.ManualEntryError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_CONFIG
    finally:
        con.close()


def cmd_batch(args: argparse.Namespace) -> int:
    con = _open(args)
    try:
        if args.action == "list":
            rows = db_mod.list_batches(con)
            if args.json:
                _print_json(rows)
            else:
                for r in rows:
                    print("{batch_id:<24} {material:<9} {mode:<8} {created_at_utc} {final_status}".format(**r))
            return EXIT_OK
        summary = manual.batch_summary(con, args.batch_id)
        if args.json:
            _print_json(summary)
        else:
            print(manual.format_summary(summary))
        return EXIT_OK
    except manual.ManualEntryError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_CONFIG
    finally:
        con.close()


def cmd_replay(args: argparse.Namespace) -> int:
    alt = None  # type: Optional[Dict[str, Any]]
    if args.config:
        alt = config_mod.load_config(args.config)
    con = db_mod.connect(resolve_db_path(args.db, args.config))
    try:
        rep = replay_report(con, args.batch_id, alt)
        if args.json:
            _print_json(rep)
        else:
            print(format_report(rep))
        return EXIT_OK
    except (ReplayError, config_mod.ConfigError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_CONFIG
    finally:
        con.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "initdb":
        return cmd_initdb(args)
    if args.command in ("sample", "quality") and args.action is None:
        parser.error("하위 명령이 필요합니다 (sample add / quality set)")
    if args.command in ("sample", "quality", "manual-stop"):
        return cmd_manual(args)
    if args.command == "batch":
        if args.action is None:
            parser.error("batch show / batch list")
        return cmd_batch(args)
    if args.command == "replay":
        return cmd_replay(args)
    parser.print_help()
    return EXIT_CONFIG
