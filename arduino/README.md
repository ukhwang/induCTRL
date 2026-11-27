# 아두이노 우노 PWM 모터 제어 (stir_pwm_controller)

교반기 DC 모터의 **속도(PWM)·감속·정지**를 아두이노 우노 R3로 담당하는 스케치입니다.
MIC(Jetson)는 USB 시리얼로 명령만 보내고, 모터 전원은 아두이노를 거치지 않고
모터 드라이버를 통해 따로 들어갑니다.

```
MIC-711D (Jetson) ──USB──> Arduino Uno R3 ──PWM/EN──> 모터 드라이버 ──> DC 모터
   pwm_serial.py                 stir_pwm_controller.ino            (L298N / BTS7960 / MD10C …)
```

| 파일 | 내용 |
|---|---|
| `arduino/stir_pwm_controller/stir_pwm_controller.ino` | 우노 R3 스케치 (핀·램프·워치독 상수는 파일 상단) |
| `inductrl_stirctl/inductrl/pwm_serial.py` | MIC 쪽 시리얼 제어 모듈 + CLI |
| `inductrl_stirctl/tests/test_pwm_serial.py` | 하드웨어 없이 도는 71개 테스트 (프로토콜·램프·정지·설정) |
| `inductrl_stirctl/config.example.json` | 선택 항목 `arduino_pwm` (기본 `enabled:false`, `port:null`) |

스케치는 `arduino-cli compile --fqbn arduino:avr:uno` 로 컴파일 확인했습니다
(프로그램 5,022바이트 15%, 전역 변수 281바이트 13%).

## 1. 현장에서 정할 것 (핀 번호·드라이버)

핀은 모두 스케치 맨 위 `1. 현장에서 바꾸는 값` 절에 모아 두었습니다. 배선만 보고
아래 표에서 고른 줄의 상수만 바꾸면 됩니다.

| 상수 | 기본값 | 뜻 |
|---|---|---|
| `PIN_PWM` | `9` | 속도 PWM 출력. 우노 PWM 핀은 3, 5, 6, 9, 10, 11 |
| `PIN_EN` | `7` | 드라이버 Enable(출력 허가). `USE_ENABLE_PIN=false` 면 안 씀 |
| `USE_ENABLE_PIN` | `true` | EN 핀이 없는 드라이버면 `false` |
| `ENABLE_ACTIVE_HIGH` | `true` | 대부분 HIGH 활성. LOW 활성 드라이버면 `false` |
| `PIN_DIR` | `8` | 방향. `USE_DIR_PIN=false` 면 안 씀 |
| `USE_DIR_PIN` | `false` | 방향을 바꿀 일이 없으면 `false`. **스케치는 방향을 스스로 바꾸지 않습니다** |
| `DIR_FORWARD_LEVEL` | `HIGH` | 정방향 레벨 (부팅 시 한 번만 설정) |
| `RAMP_ENABLED` | `true` | 부드러운 램프 업/다운 |
| `RAMP_STEP_PWM` | `5` | 한 단계 PWM 폭 (1~255) |
| `RAMP_INTERVAL_MS` | `25` | 단계 간격. 기본값이면 0→255 약 1.3초 |
| `WATCHDOG_ENABLED` | `false` | `HEARTBEAT` 미수신 시 자동 정지 (선택) |
| `WATCHDOG_TIMEOUT_MS` | `3000` | 워치독 시간 |

### 드라이버별 배선 예

| 드라이버 | 우노 → 드라이버 | 스케치 설정 |
|---|---|---|
| **L298N** (2A급, 24V는 발열 큼) | `D9 → ENA`, `D7 → IN1`, `GND → IN2`, `5V → 5V(로직)` | 기본값 그대로 (`PIN_PWM=9`, `PIN_EN=7`). IN1 이 HIGH 인 동안 ENA PWM 으로 속도 |
| **BTS7960 / IBT-2** | `D9 → RPWM`, `D7 → R_EN`, `L_EN → R_EN`, `LPWM → GND` | 기본값. `USE_DIR_PIN=false` |
| **MD10C / MD13S** | `D9 → PWM`, `D8 → DIR` | `USE_ENABLE_PIN=false`, `USE_DIR_PIN=true` |
| **MOSFET 모듈(IRF520 등)** | `D9 → SIG` | `USE_ENABLE_PIN=false`, `USE_DIR_PIN=false` |
| **릴레이(ON/OFF만)** | 이 스케치는 부적합 | 속도 제어가 필요 없으면 기존 DO 방식을 쓰세요 |

- **모터 전원과 우노 전원은 분리**하고, 드라이버 GND 와 우노 GND 만 공통으로 묶습니다.
- 모터 단자에는 **플라이백 다이오드**(또는 드라이버 내장 여부)를 확인합니다.
- L298N 은 24V 에서 전압 강하·발열이 큽니다. 24V 5A 어댑터를 쓸 계획이면 BTS7960 계열이 안전합니다.
- 우노 9·10번은 Timer1 을 씁니다. 같은 보드에서 `Servo` 라이브러리를 함께 쓰면 충돌합니다.
- 우노 PWM 주파수는 3·5·6·11번 약 490Hz, 9·10번 약 980Hz 입니다. 모터에서 윙윙 소리가 나면 정상이며, 소음이 문제면 주파수를 바꾸는 대신 드라이버·모터 쪽을 조정하세요.

## 2. 업로드

Arduino IDE: `arduino/stir_pwm_controller/stir_pwm_controller.ino` 를 열고 보드 `Arduino Uno`, 포트 선택 후 업로드.

arduino-cli:

```bash
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno arduino/stir_pwm_controller
arduino-cli upload  --fqbn arduino:avr:uno -p /dev/ttyACM0 arduino/stir_pwm_controller
```

> 업로드 직후 **모터 전원을 끈 상태**에서 시리얼로 `PING`·`STATUS`만 확인하고,
> 그 다음 낮은 듀티(10~20%)부터 모터를 돌리세요.

## 3. 시리얼 프로토콜

- USB 시리얼, **115200 baud**, ASCII, **줄바꿈(`\n`)으로 명령 끝**. `\r` 은 무시합니다.
- 한 줄에 명령 하나. 빈 줄은 조용히 무시합니다. 소문자도 받습니다(`ping` = `PING`).
- 32자를 넘는 줄은 `ERR LINE_TOO_LONG` 으로 응답하고 버립니다.

| 명령 | 응답 | 동작 |
|---|---|---|
| `PING` | `OK PONG` | 연결 확인 |
| `SET_PCT <0..100>` | `OK PCT <val>` | 목표 듀티(%). 램프가 켜져 있으면 서서히 올라감/내려감 |
| `SET_PWM <0..255>` | `OK PWM <val>` | 목표 듀티를 PWM 원값으로 직접 설정 |
| `STOP` | `OK STOPPED` | **즉시** PWM 0 + Enable 비활성 (램프 무시) |
| `STATUS` | `OK STATUS pwm=153 pct=60 target_pwm=178 target_pct=70 uptime_ms=1234 ramp=1 en=1` | 적용 PWM·퍼센트·목표값·업타임 |
| `HEARTBEAT` | `OK HEARTBEAT` | 워치독 시계 재설정 |

오류 응답:

| 응답 | 뜻 |
|---|---|
| `ERR UNKNOWN_CMD <CMD>` | 모르는 명령 |
| `ERR ARG <CMD>` | 숫자가 없거나 형식이 다름 (`SET_PCT`, `SET_PCT 6 0`) |
| `ERR RANGE <CMD> <min>..<max>` | 범위 밖 (`SET_PCT 101`, `SET_PWM 300`) |
| `ERR LINE_TOO_LONG` | 32자 초과 |

요청하지 않은 줄도 나옵니다. MIC 쪽 모듈은 이 줄을 `events` 로 보관하고 응답 대기를 계속합니다.

| 줄 | 언제 |
|---|---|
| `BOOT pwm=0 ready=1` | 전원 인가/리셋 직후 (USB 연결 시 우노가 리셋되어 놓칠 수 있음) |
| `EVT WATCHDOG_STOP pwm=0 timeout_ms=3000` | 워치독이 정지를 걸었을 때 |

## 4. 안전 동작

1. **부팅 시 PWM 0**: `setup()` 에서 출력 0, Enable 비활성으로 시작합니다. 전원이 들어와도 모터는 돌지 않습니다.
2. **STOP 은 즉시**: 램프를 기다리지 않고 `analogWrite(0)` + Enable LOW. `SET_PCT 0` 은 램프를 타고 서서히 0 이 됩니다(감속 정지).
3. **램프 한계**: `RAMP_STEP_PWM`/`RAMP_INTERVAL_MS` 로 최대 변화율이 정해집니다. 램프 중에도 `STOP` 은 즉시 먹습니다.
4. **워치독(선택)**: `WATCHDOG_ENABLED=true` 면 `HEARTBEAT`(또는 다른 명령)가 끊긴 뒤 `WATCHDOG_TIMEOUT_MS` 가 지나면 스스로 정지하고 `EVT` 를 보냅니다. MIC 쪽에서 `HEARTBEAT` 를 주기적으로 보내야 합니다.
5. **하지 않는 일**: 모터 시작, 정지 해제, 정·역회전 전환을 스스로 하지 않습니다. 정지 후 다시 돌리는 것은 항상 사람 또는 상위 프로그램의 명시적 `SET_*` 입니다.
6. **브레이크는 없습니다**: `STOP`/`SET_PCT 0` 은 드라이버를 비활성해 **관성 정지**입니다. 급제동이 필요하면 드라이버의 브레이크 기능이나 기계식 제동을 별도로 검토하세요.
7. 프로그램·통신이 죽었을 때의 최종 안전은 **설비 쪽 비상정지·인터록**이 책임집니다. 이 스케치는 그 보조입니다.

## 5. MIC 쪽 사용법

```bash
cd inductrl_stirctl
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ping          # 연결 확인
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --pct 20        # 20% (램프 업)
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ramp 20 60 --step 10 --interval 0.5
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --status
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --stop          # 즉시 정지
```

하드웨어 없이 같은 명령을 연습할 수 있습니다(가상 장치).

```bash
python3 -m inductrl.pwm_serial --simulate --pct 60 --json
```

Python 코드에서:

```python
from inductrl.pwm_serial import PwmController, PyserialTransport, PwmStopActuator

with PwmController(PyserialTransport("/dev/ttyACM0")) as pwm:
    pwm.connect()                      # 우노 리셋 대기(기본 2초) 후 PING 재시도
    pwm.set_speed_pct(40)
    pwm.ramp_speed_pct(40, 70, step_pct=10, step_interval_s=0.5)
    pwm.stop()

# 기존 Modbus 정지(actuator.py)와 함께 쓰기: 배치당 STOP 1회, 재시도 없음
stop_pwm = PwmStopActuator(pwm)
result = stop_pwm.request_stop()       # result.outcome / result.write_calls
```

`config.json` 에서 포트를 고정하려면 (선택 항목, 없어도 기존 설정·테스트는 그대로 동작):

```json
"arduino_pwm": {
  "enabled": true,
  "port": "/dev/ttyACM0",
  "baud": 115200,
  "command_timeout_s": 1.0,
  "reset_wait_s": 2.0,
  "ramp_step_pct": 5,
  "ramp_interval_s": 0.25
}
```

### 포트가 안 보일 때

```bash
ls /dev/ttyACM*                       # 우노 R3 는 보통 /dev/ttyACM0
dmesg | tail -20                      # USB 인식 로그
sudo usermod -aG dialout $USER        # 권한 (로그아웃/재로그인 필요)
python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ping
```

`Permission denied` 면 `dialout` 그룹, `포트를 열 수 없습니다` 면 케이블·드라이버(CH340 클론 보드)를 확인하세요.
`pyserial` 은 실제 포트를 열 때만 필요합니다: `python3 -m pip install pyserial`.

## 6. 현장 확인 필요 항목

1. 실제 모터 드라이버 모델과 정격(전압·연속 전류) — 24V 5A 어댑터와 맞는지
2. 드라이버의 Enable 극성(LOW 활성 여부), 방향 핀 필요 여부
3. 사용할 우노 핀 번호와 주변 배선(9·10번 Servo 충돌 여부 포함)
4. 모터 정격 PWM 하한: 너무 낮은 듀티에서 돌지 않거나 윙윙거리는 구간
5. 램프 시간(`RAMP_STEP_PWM`·`RAMP_INTERVAL_MS`)의 실제 토크 충격 — 설비에서 시험
6. 워치독 사용 여부와 시간, MIC 쪽 `HEARTBEAT` 주기
7. 정지 방식: 관성 정지로 충분한지, 브레이크/기계식 제동이 필요한지
8. 노이즈: 모터 리드와 시리얼 케이블 분리, 필요 시 페라이트 코어·차폐
9. 우노와 MIC 의 USB 케이블 길이·전원 (MIC USB 포트 전류 한계)
