# InduCTRL MIC-711D-ON2A1 ↔ ADAM-3600-C2G 교반 완료 판정·자동 정지

이 저장소에는 프로그램이 두 개 있습니다.

| 프로그램 | 역할 | Modbus 쓰기 |
|---|---|---|
| `main.py` (기존 로거) | 설정된 레지스터 원시값을 SQLite `sensor_raw` 에 저장 | **없음 (읽기 전용)** |
| `stirctl.py` (새 프로그램) | 교반 완료 판정 + 정지 요청 | **live 모드에서만, 배치당 1회 정지 쓰기** (`write_coil` 1건) |

- 새 프로그램이 하는 일은 **완료 판정**과 **정지 요청 1회**뿐입니다. 모터 시작, 재가동, DO 해제(원래 값 복귀) 코드는 어디에도 없습니다.
- Modbus 쓰기 코드는 `inductrl/actuator.py` 한 파일에만 있습니다(테스트가 이를 검사합니다).
- 독립 비상정지와 안전 인터록은 기존 설비 제어가 책임지며, 이 프로그램의 범위가 아닙니다.
- 프로그램이 죽거나 통신이 끊기면 기본 결과는 **정지 명령 없음(운영자 수동 정지)** 입니다. 종료할 때도, Ctrl+C 를 눌렀을 때도 쓰기는 없습니다.

## 파일 구성

```
.
├── main.py                 # 기존 읽기 전용 로거 (수정 안 함)
├── test_local.py           # 기존 가상 데이터 테스트 (수정 안 함)
├── stirctl.py              # 새 프로그램 진입점
├── config.example.json     # 새 구조 설정 예제 (미확인 값 null, 검증 플래그 false)
├── requirements.txt        # pymodbus==3.6.9
├── README_ko.md
├── inductrl/
│   ├── __init__.py
│   ├── models.py           # 채널·상태·이벤트 이름, 공용 데이터 구조
│   ├── config.py           # 설정 로드·검증·해시, live 시작 조건 점검
│   ├── decode.py           # 레지스터 해석(uint16/int16/uint32/int32/float32, 워드 순서)
│   ├── db.py               # SQLite(WAL, busy_timeout) 테이블과 입출력
│   ├── acquisition.py      # 한 주기 수집, 채널별 개별 요청, 주기 시간 예산
│   ├── modbus_io.py        # pymodbus 읽기 경로 + 읽기 전용 래퍼(쓰기 호출 시 예외)
│   ├── rpm.py              # 회전 카운터 → RPM (오버플로·리셋 처리)
│   ├── decision.py         # 완료 후보 판정 (범위·변동폭·coverage·유지시간)
│   ├── processing.py       # 원시값 → 유효성 → RPM → 판정 (run 과 replay 가 공용)
│   ├── stopctl.py          # 정지 확인 상태기계 (쓰기 없음)
│   ├── actuator.py         # ★ 유일한 Modbus 쓰기 코드: 배치당 write_coil 1회
│   ├── pwm_serial.py       # 아두이노 우노 PWM 속도·감속·정지 시리얼 제어 (Modbus 쓰기 없음)
│   ├── mock.py             # 가상 센서·가상 모터·시험 시나리오
│   ├── sim.py              # 실시간 시뮬레이터 물리 모델(물·용질 → 혼합·동력·에너지)
│   ├── runner.py           # 배치 실행 루프 (mock / observe / live)
│   ├── replay.py           # 저장된 원시값·설정으로 재계산
│   ├── manual.py           # 수동 입력·배치 요약
│   ├── timeutil.py         # UTC 기록용 시각, 가상 시계
│   └── cli.py              # 명령행
└── tests/                  # unittest (하드웨어 없이 실행)
    ├── helpers.py
    ├── test_units.py       # 해석·RPM·판정 엔진·설정
    ├── test_scenarios.py   # mock 시나리오 전 과정
    ├── test_modes.py       # observe/live 경로, 액추에이터, live 시작 거부
    ├── test_pwm_serial.py  # 아두이노 PWM 프로토콜·램프·정지·설정 (하드웨어 불필요)
    └── test_replay_cli.py  # replay, 수동 입력 CLI, 기존 test_local.py, 쓰기 코드 위치
```

## 0. 설치 전

- MIC 제품명: MIC-711D-ON2A1. 원래 JetPack 5.1.2 / Ubuntu 20.04 계열이나 **실물 부팅 후 확인**.
- 전원은 **정확한 모델과 극성에 맞는 제조사 승인 DC 전원**만 사용. 사진의 `DC OUT`는 입력이 아님. 기존 24V 모터 어댑터로 임의 급전하지 않기.
- ADAM은 10~30 VDC, MIC와 서로 전원 분리. 카메라, 모터 드라이버 등은 처음에는 연결하지 않아도 됨.
- 첫 단계는 모터 전원 완전 분리. ADAM과 MIC Ethernet 케이블 연결. 단독 연결 또는 LAN 스위치 권장.

## 1. MIC에서 현재 OS 확인

```bash
cat /etc/os-release
cat /etc/nv_tegra_release
python3 --version
ip -br addr
lsblk
```

업그레이드/포맷은 진행하지 마세요.

## 2. iRTU Studio에서 ADAM 설정

1. Windows PC에서 iRTU Studio로 ADAM에 접속. 제공 장비의 실제 IP, 네트워크, 펌웨어를 확인.
2. ADAM LAN IP를 고정(예시: `192.168.10.20/24`), MIC LAN IP 예시 `192.168.10.10/24`. IP는 **임의의 초기 IP를 가정하면 안 됩니다**.
3. `Data Center`에서 **실제 입력 태그**(탁도 상·하단, 전도도, 회전 카운터)를 구성하고, `Protocol Service`에서 **Modbus TCP Server (기본적으로 502)** 활성화.
4. `Modbus Address Mapping`에서 태그마다 **주소, 타입(FC03 또는 FC04), 16/32비트 형식, 부호, 워드 순서**를 기록.
5. 센서나 기지 전압으로 태그 값이 변화하는지 안전한 방법으로 검증; 실제 출력을 전기적으로 확인한 뒤에만 해당 채널의 `mapping_verified`를 `true`로 설정.

※ ADAM 문서 예시의 30001은 Input Register 표기입니다. Python 라이브러리에 전달하는 주소는 통상 0부터 세지만 **실기기 테스트로 확인**하세요. 본 프로젝트는 주소를 무조건 지정하지 않았습니다(예제는 모두 null).

## 3. 설치

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

외부 의존성은 `pymodbus==3.6.9` 하나입니다. Python 3.8 이상에서 동작합니다(3.8.20 + pymodbus 3.6.9 에서 전체 테스트 통과 확인).

## 4. 기존 로거(main.py) — 읽기 전용

기존 사용법은 그대로입니다. `main.py` 는 설정 파일의 **최상위** `function_code`, `address_zero_based`, `register_count`, `mapping_verified`, `interval_seconds` 만 읽습니다(새 예제 파일에도 그대로 남겨 두었습니다). `main.py` 에는 DO 쓰기가 없습니다.

```bash
python main.py --mode mock --samples 5
# 실기기 읽기: config.json 의 최상위 매핑 값을 확인·수정하고 mapping_verified=true 후
ping -c 3 192.168.10.20
python main.py --mode live --config config.json --samples 10 --batch-id initial-test
```

원시 레지스터를 곧바로 `RPM`, `V`, `A`, `NTU`라고 표시하면 안 됩니다. 새 프로그램은 채널별 `data_type`, `scale`, `offset`, `unit` 이 확인된 경우에만 공학값으로 바꿉니다.

## 5. 새 프로그램(stirctl.py) — 모드

| 모드 | Modbus 클라이언트 | 쓰기 | 용도 |
|---|---|---|---|
| `mock` (기본) | **만들지 않음** | 가상 액추에이터만 | 후보 판정 → 정지 요청 → 정지 확인 전 과정 시험 |
| `observe` | 읽기 전용 래퍼 | **0회** (쓰기 메서드 호출 시 즉시 예외) | 실제 ADAM 값으로 판정하고 후보 시각만 저장 |
| `live` | 읽기용 + **별도** 쓰기용 | 배치당 `write_coil` **1회** | 모든 검증 조건을 갖췄을 때만 |

### mock

```bash
# 가상 시계로 대기 없이 (기본 240주기 = 가상 120초)
python stirctl.py run --mode mock --material saltwater --batch-id demo-1 --fast
python stirctl.py run --mode mock --material caco3 --batch-id demo-2 --fast --scenario counter_reset
# 실제 시간으로 (Ctrl+C 까지)
python stirctl.py run --mode mock --material saltwater --batch-id demo-3
```

mock 은 설정에서 null 인 값만 **가상값**으로 채워 실행합니다(주소, 형식, 임계값 등). 채운 항목은 `batches.config_json` 의 `_mock_filled` 에 기록되며, 검증 플래그는 켜지 않습니다. 따라서 mock 후보는 보통 '미검증 판정'으로 표시되지만, mock 의 정지는 가상이므로 정지 시퀀스까지 진행합니다.

시나리오(`--scenario`):

| 이름 | 내용 |
|---|---|
| `normal` | 정상 완료: 수렴 → 후보 → 정지 요청 → 정지 확인 |
| `early_flat` | 초반부터 평탄·범위 안 → min_stir 전에는 후보 없음 |
| `out_of_range` | 목표 범위 밖에서 안정 → 후보 없음 |
| `dropout` | 판정 채널 4회 중 1회 수신 누락 → coverage 부족, 판정 보류 |
| `stale` | 20초 이후 판정 채널 수신 끊김 → READ_ERROR 후 STALE |
| `invalid_value` | 5회 중 1회 센서 고장값 → OUT_OF_RANGE (값 NULL) |
| `counter_overflow` | 회전 카운터 래핑 → RPM 정상 유지 |
| `counter_reset` | 20초에 카운터 리셋 → COUNTER_RESET, 구간 동안 RPM 무효 |
| `write_exception` | 정지 쓰기 Modbus 예외 응답 → stop_unconfirmed |
| `write_timeout` | 정지 쓰기 무응답(모터는 실제로 멈춤) → stop_unconfirmed + stop_observed |
| `write_connection_error` | 정지 쓰기 연결 오류 → stop_unconfirmed |
| `no_rpm_drop` | 쓰기 성공 응답, RPM 은 안 떨어짐 → 타임아웃 후 stop_unconfirmed |
| `slow_reads` | 채널당 0.15초 지연 → SKIPPED_BUDGET, overrun |

### observe

```bash
cp config.example.json config.json
# config.json 의 channels.* 를 iRTU Studio 매핑대로 채우고, 확인된 채널만 mapping_verified=true
python stirctl.py run --mode observe --config config.json --material caco3 --batch-id obs-001
```

- 주소가 null 이거나 `mapping_verified=false` 인 채널은 `UNMAPPED` 로 기록됩니다. 현재 재료의 판정에 필요한 채널이 UNMAPPED 면 판정하지 않습니다(warning 이벤트).
- 임계값이 null 이거나 `thresholds_verified=false` 이면 판정은 하되 후보를 **'미검증 판정'** 으로 표시합니다. null 인 임계 조건은 적용되지 않습니다.
- `running_rpm_min` 이 null 이면 '실제 가동 확인' 조건을 판단할 수 없으므로 판정하지 않습니다.

### live

다음이 **모두** 맞아야 시작합니다. 하나라도 빠지면 빠진 항목을 모두 출력하고 종료코드 2로 시작을 거부합니다(Modbus 연결도 만들지 않음).

- 판정에 필요한 채널(재료 채널 + rotation_counter) 모두 `mapping_verified=true`, 필요한 형식 항목이 null 아님
- `decision.thresholds_verified=true`, 해당 재료의 임계값과 `running_rpm_min` 이 null 아님
- `actuator.live_enabled=true`, `actuator.verified=true`, `stop_coil_address`·`stop_value`(true/false 확정)·`stop_confirm_timeout_s`·`stopped_rpm_max`·`stop_hold_seconds` 가 null 아님
- 실행 인자 `--arm-stop`

```bash
python stirctl.py run --mode live --config config.json --material saltwater --batch-id run-001 --arm-stop
```

### 실시간 시뮬레이터 (mock + 물·용질 투입)

웹 화면에서 고른 실험 조건으로 mock 배치를 돌립니다. 실제 장비는 쓰지 않습니다.

```bash
# 물 10 L + 소금 500 g, 판정 엔진(RULE)으로 실행. 10배속이면 실제 5초에 끝납니다.
python stirctl.py run --mode mock --material saltwater --batch-id sim-1 --db /tmp/inductrl-sim.sqlite \
  --water-l 10 --solute-g 500 --control-mode rule --speed-factor 10

# 탄산칼슘 + AI 제어, 1배속(실시간)
python stirctl.py run --mode mock --material caco3 --batch-id sim-2 --db /tmp/inductrl-sim.sqlite \
  --water-l 8 --solute-g 300 --control-mode ai
```

| 인자 | 뜻 | 범위 |
|---|---|---|
| `--water-l` | 물 투입량(L) | 0.5 ~ 200 (기본 10) |
| `--solute-g` | 용질 투입량(g) | 0 ~ 20000 (기본 500) |
| `--control-mode` | `fixed` / `rule` / `ai` (기본 `rule`) | — |
| `--speed-factor` | 설비 시간 대비 실제 시간 배율 | 0.25 ~ 50 (기본 1) |
| `--fixed-seconds` | FIXED 기준 운전 시간(초) | 10 ~ 3600 (기본 120) |

`--speed-factor` 는 기다리는 시간만 줄이고 **기록되는 경과시간(과 에너지)** 은 설비 시간 그대로입니다(10배속 60초 배치 → 실제 6초, 기록 60초).

계산 모델(`inductrl/sim.py`, 기준 10 L·50 g/L·300 RPM):

- 부하계수 `Lf = (농도/50)^0.55`, 부상 임계 `Njs = 190·Lf` — 이보다 느리면 침전물이 뜨지 않습니다(경고 이벤트).
- 혼합 `dM/dt = 0.9·(rpm/Njs)^1.4·(1-M)/τ`, `τ = 14 s·(V/10)^0.35·√Lf`.
- 축동력 `P = 120 W·(V/10)^0.67·(1+0.35(Lf-1))·(rpm/300)^2.85·(1+0.22(1-M))`, 에너지 `∫P dt` [Wh].
- 센서: 염수는 전도도 `50 + 6(1-M)`, CaCO₃ 는 상부 `500 - 170(1-M)` / 하부 `500 + 170(1-M)` + 교반 난류 잡음.
- 제어 모드: `fixed` = 400 RPM·고정 시간·자동 정지 없음(비교 기준), `rule` = 320 RPM 고정, `ai` = 초기 진단 260 → 부상 단계 → 감속 순환.

## 6. 판정 요약

- 주기: `decision.interval_seconds`(초기 0.5초). 경과시간·윈도우·유지시간·타임아웃은 전부 `time.monotonic()` 기준, UTC 는 기록용입니다.
- 주소 연속을 가정하지 않고 채널별로 개별 요청합니다. 읽는 순서: rotation_counter → 판정 채널 → 나머지 → 정지 피드백. 한 주기의 읽기가 `cycle_budget_s` 를 넘으면 남은 채널은 `SKIPPED_BUDGET`, `overrun` 이벤트를 남깁니다.
- 읽기·쓰기 클라이언트 모두 pymodbus 내부 재시도를 끕니다(`retries=0`, `retry_on_empty=False`, `retry_on_invalid=False`). 따라서 `read_timeout_s` 가 요청 1건의 상한입니다.
- 무효값(READ_ERROR, STALE, OUT_OF_RANGE, DECODE_ERROR, UNMAPPED, SKIPPED_BUDGET, COUNTER_RESET 등)은 0 으로 바꾸지 않고 값 NULL + 상태로 남깁니다. 원시 레지스터는 `raw_json` 에 남습니다.
  - 읽기 실패가 마지막 정상 수신 후 `stale_seconds` 를 넘으면 `STALE` 로 기록합니다. `SKIPPED_BUDGET`·`UNMAPPED` 는 원인을 그대로 남깁니다.
- RPM: `delta = (new − old) mod 2^counter_bits`. `max_rpm` 과 경과시간으로 허용되는 펄스 수(+1 펄스 여유)를 넘으면 `COUNTER_RESET` 이벤트, 그 지점이 `rpm_window_seconds` 구간 안에 있는 동안 RPM 무효. RPM = 구간 누적 펄스 / pulses_per_rev / 구간 길이 × 60, 구간 양 끝 샘플이 모두 유효해야 합니다.
- 배치 시작(t0): `--start-trigger rpm`(기본) 은 유효 RPM ≥ running_rpm_min 이 running_confirm_seconds 동안 지속된 시점, `launch` 는 실행 시점. `batch_started` 이벤트.
- 판정 조건(모두 충족): t0 후 `min_stir_seconds` 경과 / 현재 유효 RPM ≥ running_rpm_min / `window_seconds` 구간이 t0 이후로 다 채워짐 / 판정 채널마다 구간 내 유효 샘플 수 ≥ (window/interval) × min_coverage / 판정 채널 현재 샘플 유효 / 범위·변동폭 조건.
  - saltwater: 구간 내 전도도 전부 [min, max], 최댓값 − 최솟값 ≤ conductivity_max_range
  - caco3: 상·하단 각각 범위 안, 각 센서 최댓값 − 최솟값 ≤ turbidity_max_range, 구간 평균 |상단 − 하단| ≤ top_bottom_max_diff
- 모든 조건이 `hold_seconds` 동안 연속이면 배치당 첫 후보를 1회 기록(`candidate`). 조건이 하나라도 깨지거나 판정 채널 샘플이 한 번이라도 무효면 유지시간을 0 으로 초기화(`hold_reset` 이벤트). 후보 이후에도 수집·저장은 계속합니다. 주기별 판정 상태와 사유는 `decision_log` 에 남습니다.

## 7. 정지 시퀀스 (live, `actuator.py`)

1. 후보 발생 → `stop_requested` 이벤트를 먼저 저장(commit) → `write_coil(stop_coil_address, stop_value, slave=unit_id)` 정확히 1회. 배치당 중복 발행 없음, 자동 재시도 없음.
2. 결과를 `modbus_response` 이벤트로 기록(success / exception_response / timeout / connection_error / echo_mismatch, 응답 시각 포함).
3. 결과가 불명확(타임아웃·연결 오류·예외)하면 즉시 `stop_unconfirmed` + stderr 경고. 이후 RPM 이 멈춘 것으로 보이면 `stop_observed` 만 기록하고 최종 상태는 '자동 정지 확인'이 아닙니다.
4. 쓰기 성공 후에도 읽기는 계속. `stop_confirm_timeout_s` 안에 (a) 유효 RPM ≤ stopped_rpm_max 가 stop_hold_seconds 동안 유지, 또는 (b) 검증된 정지 피드백이 stopped_value 를 보고 하면 `stop_confirmed`, 배치 최종 상태 `AUTO_STOP_CONFIRMED`(자동 정지 확인). 아니면 `stop_unconfirmed`.
5. DO 를 원래 값으로 되돌리는 쓰기는 하지 않습니다.

배치 최종 상태: `AUTO_STOP_CONFIRMED`, `STOP_UNCONFIRMED`, `CANDIDATE_RECORDED`, `CANDIDATE_UNVERIFIED`, `NO_CANDIDATE`, `INTERRUPTED`, `ABORTED_ERROR`, (`RUNNING` 이 남아 있으면 비정상 종료).

## 8. 수동 입력 CLI

DB 경로는 `--db` 로 주거나, `--config` 파일의 `database` 를 씁니다(둘 다 없으면 `config.example.json` 의 값).
시각은 ISO 8601 로 입력합니다(`Z` 또는 `+09:00` 같은 시간대 권장, 없으면 MIC 로컬 시간대로 해석해 UTC 로 저장).

```bash
# 시료 측정값 (온도·전력 등 자동수집 경로가 확정되지 않은 값 포함)
python stirctl.py sample add --batch-id run-001 --position top --metric turbidity --value 512 --unit NTU --measured-at 2026-12-01T14:03:05+09:00
python stirctl.py sample add --batch-id run-001 --position mid --metric temperature --value 21.5 --unit C
# 품질 합격 여부 (마지막 입력이 최종)
python stirctl.py quality set --batch-id run-001 --passed yes --note "육안 균일"
# 운영자 수동 정지 시각 (쓰기 없음, 기록만)
python stirctl.py manual-stop --batch-id run-001
python stirctl.py manual-stop --batch-id run-001 --at 2026-12-01T14:05:10+09:00
# 배치 요약 / 목록
python stirctl.py batch show --batch-id run-001
python stirctl.py batch list
```

`run` 과 수동 입력이 같은 DB 에 동시에 써도 되도록 WAL 모드와 `busy_timeout=5000ms` 를 씁니다. `--json` 을 붙이면 JSON 으로 출력합니다.

## 9. 재생(replay)

```bash
python stirctl.py replay --batch-id run-001 --db data/inductrl_readings.sqlite3
# 다른 설정으로 계산한 결과도 나란히 비교
python stirctl.py replay --batch-id run-001 --db data/inductrl_readings.sqlite3 --config config.try.json
```

DB 에 저장된 원시값(`readings.raw_json`)과 그 배치의 저장된 설정(`batches.config_json`)으로 RPM 과 완료 후보 시각을 다시 계산해, 원래 기록된 이벤트(candidate / stop_requested / modbus_response / stop_confirmed / stop_unconfirmed), 수동 정지 시각, 시료 측정값, 품질 합격 여부와 함께 보여 줍니다. 원래 후보 시각과의 일치 여부도 표시합니다. 기존 `sensor_raw` 로그는 채널 정보가 없어 재생 대상이 아닙니다.

주의: 다른 설정으로 재계산해도 원래 실행 중 실제로 정지된 이후의 데이터는 바뀌지 않습니다(예: 원래 실행이 38초에 정지했다면 그보다 늦은 후보는 재현되지 않음).

## 10. SQLite 테이블

| 테이블 | 내용 |
|---|---|
| `batches` | batch_id, material, mode, start_trigger, scenario, created/finished_at_utc, config_json(배치 시작 시 설정 전체), config_hash(SHA-256), final_status |
| `readings` | batch_id, seq, channel(+ 파생 `rpm`, `stop_feedback`), collected_at_utc, monotonic_s, elapsed_s, raw_json, value, unit, status, error_detail |
| `events` | batch_id, event_type, at_utc, monotonic_s, elapsed_s, detail_json |
| `decision_log` | 주기별 판정 상태, RPM, 유지시간, 검증 여부, 미충족 사유 |
| `manual_entries` | batch_id, kind(sample/quality/manual_stop), position, metric, value, unit, passed, entered_at_utc, measured_at_utc, monotonic_s, note |
| `sensor_raw` | 기존 main.py 전용 (변경 없음) |
| `sim_runs` | 시뮬레이터 실험 조건(물·용질·농도·제어 모드·속도·Njs·FIXED 기준 에너지) |
| `sim_metrics` | 주기별 시뮬레이터 계측(축동력 W, 누적 Wh, 잔여 불균일 CV%, 혼합도, 단계) |

## 11. 아두이노 PWM 속도 제어 (선택)

교반 모터를 **ADAM DO 릴레이 온/오프**가 아니라 **PWM 듀티로 속도를 바꾸며 감속·정지**하려면
아두이노 우노 R3(`arduino/stir_pwm_controller/stir_pwm_controller.ino`)를 USB 로 연결해 씁니다.
ADAM/Modbus 경로는 그대로 두고, 속도 제어만 아두이노가 맡는 구조입니다.

- **배선·프로토콜·핀 상수·드라이버별 배선·현장 확인 항목**은 [arduino/README.md](../arduino/README.md) 에 정리했습니다.
- MIC 쪽 모듈은 `inductrl/pwm_serial.py` 이고, 표준 라이브러리만으로 동작합니다(pyserial 은 실제 포트를 열 때만 필요).
- 부팅 시 PWM 0(안전 정지), `STOP` 은 즉시 PWM 0 + Enable 비활성, `SET_PCT 0` 은 램프를 타는 감속 정지입니다.
- `PwmStopActuator` 는 `actuator.py` 의 `StopActuator` 규격을 따르며 **배치당 STOP 1회, 재시도 없음**입니다. 기존 Modbus 정지와 함께 쓸 수 있습니다.
- 설정은 `config.example.json` 의 선택 항목 `arduino_pwm`(`enabled` 기본 `false`, `port` 기본 `null`, `baud` `115200`)입니다. 이 항목을 지우거나 넣지 않아도 기존 로더·테스트는 그대로 동작합니다.

```bash
cd inductrl_stirctl
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ping
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --pct 20
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ramp 20 60 --step 10 --interval 0.5
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --stop
python3 -m inductrl.pwm_serial --simulate --status          # 하드웨어 없이 연습
```

## 12. 테스트

```bash
python -m unittest discover -s tests -t . -v   # 새 테스트 (하드웨어 불필요, 약 5~10초)
python test_local.py                           # 기존 테스트
```

현재 154개(기존 83개 + PWM 시리얼 71개)가 통과합니다.
새 테스트는 가상 시계를 써서 수 분짜리 배치를 몇 초 안에 돌립니다. 확인하는 항목: 정상 완료 이벤트 순서와 시각, min_stir 전 후보 없음, 범위 이탈, 수신 누락·coverage 부족과 유지시간 초기화, stale·범위 밖 값의 NULL 처리, 카운터 오버플로·리셋, 쓰기 실패(예외·타임아웃·연결 오류)와 재시도 0회, 쓰기 성공 후 RPM 미하강, 배치당 정지 요청 1회, observe 쓰기 0회, mock 에서 Modbus 클라이언트 미생성, live 시작 거부(각 조건별), replay 후보 시각 일치, 기존 test_local.py 통과, 쓰기 코드가 actuator.py 에만 있는지, 그리고 PWM 시리얼의 명령 문자열·범위 검증·응답 해석·PING 재시도·램프 단계·STOP 1회·설정 스키마 호환입니다. pymodbus 가 설치되어 있으면 실제 pymodbus 클라이언트로 응답 없는 로컬 소켓에 정지 쓰기를 보내 **프레임이 1개만 전송됨**도 확인합니다.

## 참고

- Advantech ADAM-3600-C2G 공식 사용자 매뉴얼: https://downloadt.advantech.com/ProductFile/Downloadfile5/1-1A68V8J/ADAM-3600-C2G_User_Manual_Ed.2-D1.pdf
- MIC-711D-ON 제품 사양: https://www.advantech.com/ko-kr/products/965e4edb-fb98-429e-89ed-9a0a8435a7be/mic-711d-on/mod_bc1c2907-587f-4801-81be-8dc23e4a8bc9
- PyModbus 3.6.9 클라이언트 API: https://pymodbus.readthedocs.io/en/v3.6.9/source/client.html

## 현장 확인 필요 항목

1. 채널별 실제 주소, 레지스터 종류(FC03/FC04), 데이터 형식(16/32비트, 부호, float), 워드 순서, 환산식과 단위 (iRTU Studio Modbus Address Mapping 기준)
2. 정지 DO의 코일 주소, 쓰기 가능 여부, 정지값(True/False), 실제 배선과 동작 논리
3. 정지 신호 방식(유지 vs 펄스) 합의와 DO 해제 주체(작업자 수동 / 기존 제어반). 해제하지 않았을 때 다음 배치에서 시작이 가능한지
4. ADAM 통신이 끊겼을 때 DO 안전값(watchdog) 설정
5. 정지 피드백 신호 유무와 주소
6. 회전 센서의 한 바퀴당 펄스 수, 카운터 비트 수, ADAM DI 주파수 모드 지원 여부
7. 전도도 센서의 온도보상(ATC) 내장 여부 (온도 자동수집 미확정)
8. 탁도·전도도 센서의 고장 시 출력값(valid_min/valid_max 결정용)
9. 실험으로 확정할 값: 품질 범위, 허용 변동폭, 상·하단 허용 차이, running_rpm_min, stopped_rpm_max, 각종 시간 설정
10. MIC 시스템 시계의 NTP 동기화 여부 (수동 입력 시각과 대조용)
11. 0.5초 주기에서 채널 4개를 읽는 실제 소요 시간과 cycle_budget_s 적정값
