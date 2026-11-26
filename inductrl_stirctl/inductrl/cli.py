"""stirctl 명령행.

  run         배치 실행 (mock / observe / live)
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
    opts = RunOptions(mode=args.mode, material=args.material, batch_id=args.batch_id,
                      start_trigger=args.start_trigger, arm_stop=args.arm_stop, samples=args.samples,
                      scenario=args.scenario, fast=args.fast, db_path=args.db,
                      status_every=args.status_every)
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
