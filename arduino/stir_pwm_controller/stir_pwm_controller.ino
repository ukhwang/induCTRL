/*
 * stir_pwm_controller — InduCTRL 교반기 DC 모터 PWM 속도 제어 (감속·정지)
 *
 * 대상 보드: Arduino Uno R3 (ATmega328P, 16 MHz)
 * 통신: USB 시리얼 115200 baud, 줄바꿈(\n)으로 끝나는 ASCII 명령
 *
 * 이 스케치가 하는 일
 *   - 목표 듀티(0~100% 또는 0~255)를 받아 모터 드라이버에 PWM 을 출력합니다.
 *   - 부드러운 램프 업/다운(기본 켜짐)으로 급격한 토크 충격을 줄입니다.
 *   - STOP 은 램프를 무시하고 즉시 PWM 0 + Enable 비활성으로 정지합니다.
 *   - 부팅 직후에는 항상 PWM 0 (안전 정지 상태)에서 시작합니다.
 *   - (선택) 워치독: HEARTBEAT 가 끊기면 자동 정지합니다.
 *
 * 이 스케치가 하지 않는 일
 *   - 모터 정·역회전 전환, 자동 재가동, 정지 해제를 스스로 하지 않습니다.
 *     방향 핀(DIR)은 setup 에서 한 번만 설정합니다.
 *
 * 프로토콜·배선 안내는 같은 저장소의 arduino/README.md 를 보세요.
 * 명령을 보내는 쪽은 inductrl/pwm_serial.py 입니다.
 */

// ===========================================================================
// 1. 현장에서 바꾸는 값 — 핀 번호와 모터 드라이버 방식
// ===========================================================================
// PWM 핀: 우노의 PWM 핀은 3, 5, 6, 9, 10, 11 입니다.
//   9·10번은 Timer1(~980 Hz), 3·5·6·11번은 Timer0/2(~490 Hz)를 씁니다.
//   9·10번을 Servo 라이브러리와 함께 쓰면 충돌합니다.
const uint8_t PIN_PWM = 9;

// Enable(출력 허가) 핀. 드라이버에 EN/ENA 가 없으면 USE_ENABLE_PIN = false.
const uint8_t PIN_EN = 7;
const bool USE_ENABLE_PIN = true;
const bool ENABLE_ACTIVE_HIGH = true;   // 대부분의 L298N·MD10C·BTS7960 계열은 HIGH 활성

// 방향 핀. 방향을 바꿀 일이 없으면 USE_DIR_PIN = false 로 두고 배선하지 않습니다.
const uint8_t PIN_DIR = 8;
const bool USE_DIR_PIN = false;
const bool DIR_FORWARD_LEVEL = HIGH;

// 상태 표시 LED (우노 내장 13번). 시리얼 통신 중 깜빡입니다.
const uint8_t PIN_LED = LED_BUILTIN;

// ===========================================================================
// 2. 동작 값 — 램프와 워치독
// ===========================================================================
// 부드러운 램프 업/다운. RAMP_ENABLED=false 이면 목표값으로 즉시 점프합니다.
const bool RAMP_ENABLED = true;
const uint8_t RAMP_STEP_PWM = 5;        // 한 번에 바뀌는 PWM 폭 (1~255)
const uint16_t RAMP_INTERVAL_MS = 25;   // 이 간격마다 한 단계 (0→255 약 1.3초)

// (선택) 워치독: HEARTBEAT 를 WATCHDOG_TIMEOUT_MS 동안 못 받으면 자동 정지.
// 현장에서 통신 두절 시 정지를 원하면 true 로 바꾸세요. 기본은 꺼져 있습니다.
const bool WATCHDOG_ENABLED = false;
const uint32_t WATCHDOG_TIMEOUT_MS = 3000UL;

// 줄 버퍼 길이. 넘치는 줄은 ERR LINE_TOO_LONG 으로 응답하고 버립니다.
const size_t LINE_MAX = 32;

const unsigned long SERIAL_BAUD = 115200;

// ===========================================================================
// 3. 상태
// ===========================================================================
int appliedPwm = 0;       // 실제로 analogWrite 에 넣은 값 (0~255)
int targetPwm = 0;        // 목표 값 (0~255)
bool outputEnabled = false;  // STOP 이후 false. SET_* 를 받으면 true.
unsigned long lastRampMs = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long bootMs = 0;

char lineBuf[LINE_MAX + 1];
size_t lineLen = 0;
bool lineOverflow = false;

// ===========================================================================
// 4. 설정 적용
// ===========================================================================
void applyEnablePin() {
  if (!USE_ENABLE_PIN) {
    return;
  }
  bool on = outputEnabled && (targetPwm > 0 || appliedPwm > 0);
  digitalWrite(PIN_EN, (on == ENABLE_ACTIVE_HIGH) ? HIGH : LOW);
}

void writePwm(int value) {
  if (value < 0) {
    value = 0;
  }
  if (value > 255) {
    value = 255;
  }
  appliedPwm = value;
  analogWrite(PIN_PWM, (uint8_t)appliedPwm);
}

// 즉시 정지: 램프를 기다리지 않고 PWM 0, Enable 비활성.
void emergencyStop() {
  outputEnabled = false;
  targetPwm = 0;
  writePwm(0);
  if (USE_ENABLE_PIN) {
    digitalWrite(PIN_EN, ENABLE_ACTIVE_HIGH ? LOW : HIGH);
  }
}

int pctToPwm(long pct) {
  // 0~100% → 0~255 (반올림)
  return (int)((pct * 255L + 50L) / 100L);
}

int pwmToPct(int pwm) {
  return (int)(((long)pwm * 100L + 127L) / 255L);
}

// ===========================================================================
// 5. 명령 처리
// ===========================================================================
void replyOk(const __FlashStringHelper *msg) {
  Serial.print(F("OK "));
  Serial.println(msg);
}

void replyUnknown(const char *cmd) {
  Serial.print(F("ERR UNKNOWN_CMD "));
  Serial.println(cmd);
}

void replyRange(const char *cmd, long lo, long hi) {
  Serial.print(F("ERR RANGE "));
  Serial.print(cmd);
  Serial.print(' ');
  Serial.print(lo);
  Serial.print(F(".."));
  Serial.println(hi);
}

void replyArg(const char *cmd) {
  Serial.print(F("ERR ARG "));
  Serial.println(cmd);
}

// 앞뒤 공백을 건너뛴 숫자 하나를 엄격하게 해석합니다.
// 뒤에 다른 문자가 붙어 있으면(예: "60x", "6 0") 실패로 봅니다.
bool parseLong(const char *text, long *out) {
  while (*text == ' ' || *text == '\t') {
    text++;
  }
  if (*text == '\0') {
    return false;
  }
  char *end = NULL;
  long value = strtol(text, &end, 10);
  if (end == text) {
    return false;
  }
  while (*end == ' ' || *end == '\t') {
    end++;
  }
  if (*end != '\0') {
    return false;
  }
  *out = value;
  return true;
}

void handleSetPct(const char *arg) {
  long pct = 0;
  if (!parseLong(arg, &pct)) {
    replyArg("SET_PCT");
    return;
  }
  if (pct < 0 || pct > 100) {
    replyRange("SET_PCT", 0, 100);
    return;
  }
  targetPwm = pctToPwm(pct);
  outputEnabled = true;
  if (!RAMP_ENABLED) {
    writePwm(targetPwm);
  }
  applyEnablePin();
  Serial.print(F("OK PCT "));
  Serial.println(pct);
}

void handleSetPwm(const char *arg) {
  long pwm = 0;
  if (!parseLong(arg, &pwm)) {
    replyArg("SET_PWM");
    return;
  }
  if (pwm < 0 || pwm > 255) {
    replyRange("SET_PWM", 0, 255);
    return;
  }
  targetPwm = (int)pwm;
  outputEnabled = true;
  if (!RAMP_ENABLED) {
    writePwm(targetPwm);
  }
  applyEnablePin();
  Serial.print(F("OK PWM "));
  Serial.println(pwm);
}

void handleStatus() {
  Serial.print(F("OK STATUS pwm="));
  Serial.print(appliedPwm);
  Serial.print(F(" pct="));
  Serial.print(pwmToPct(appliedPwm));
  Serial.print(F(" target_pwm="));
  Serial.print(targetPwm);
  Serial.print(F(" target_pct="));
  Serial.print(pwmToPct(targetPwm));
  Serial.print(F(" uptime_ms="));
  Serial.print(millis() - bootMs);
  Serial.print(F(" ramp="));
  Serial.print(RAMP_ENABLED ? 1 : 0);
  Serial.print(F(" en="));
  Serial.print(outputEnabled ? 1 : 0);
  Serial.println();
}

void handleLine(char *line) {
  // 앞뒤 공백 제거
  while (*line == ' ' || *line == '\t') {
    line++;
  }
  size_t len = strlen(line);
  while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t')) {
    line[--len] = '\0';
  }
  if (len == 0) {
    return;  // 빈 줄은 조용히 무시합니다.
  }

  // 상태를 바꾸는 명령을 받았으므로 워치독 시계를 다시 맞춥니다.
  lastHeartbeatMs = millis();
  digitalWrite(PIN_LED, HIGH);

  // 명령 토큰만 대문자로 복사합니다(소문자 입력도 허용).
  char command[16];
  size_t i = 0;
  while (i < sizeof(command) - 1 && line[i] != '\0' && line[i] != ' ') {
    char c = line[i];
    command[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    i++;
  }
  command[i] = '\0';
  const char *arg = line + i;

  if (strcmp(command, "PING") == 0) {
    replyOk(F("PONG"));
  } else if (strcmp(command, "SET_PCT") == 0) {
    handleSetPct(arg);
  } else if (strcmp(command, "SET_PWM") == 0) {
    handleSetPwm(arg);
  } else if (strcmp(command, "STOP") == 0) {
    emergencyStop();
    replyOk(F("STOPPED"));
  } else if (strcmp(command, "STATUS") == 0) {
    handleStatus();
  } else if (strcmp(command, "HEARTBEAT") == 0) {
    replyOk(F("HEARTBEAT"));
  } else {
    replyUnknown(command);
  }
}

// ===========================================================================
// 6. 주기 동작
// ===========================================================================
void rampTick() {
  if (!outputEnabled || appliedPwm == targetPwm) {
    return;
  }
  unsigned long now = millis();
  if (now - lastRampMs < RAMP_INTERVAL_MS) {
    return;
  }
  lastRampMs = now;
  int step = RAMP_STEP_PWM;
  if (step < 1) {
    step = 1;
  }
  if (appliedPwm < targetPwm) {
    writePwm(min(targetPwm, appliedPwm + step));
  } else {
    writePwm(max(targetPwm, appliedPwm - step));
  }
  applyEnablePin();
}

void watchdogTick() {
  if (!WATCHDOG_ENABLED || !outputEnabled) {
    return;
  }
  unsigned long now = millis();
  if (now - lastHeartbeatMs < WATCHDOG_TIMEOUT_MS) {
    return;
  }
  emergencyStop();
  Serial.print(F("EVT WATCHDOG_STOP pwm=0 timeout_ms="));
  Serial.println((unsigned long)WATCHDOG_TIMEOUT_MS);
}

// ===========================================================================
// 7. setup / loop
// ===========================================================================
void setup() {
  // 안전 정지 상태로 시작: 출력 0, Enable 비활성.
  pinMode(PIN_PWM, OUTPUT);
  analogWrite(PIN_PWM, 0);
  if (USE_ENABLE_PIN) {
    pinMode(PIN_EN, OUTPUT);
    digitalWrite(PIN_EN, ENABLE_ACTIVE_HIGH ? LOW : HIGH);
  }
  if (USE_DIR_PIN) {
    pinMode(PIN_DIR, OUTPUT);
    digitalWrite(PIN_DIR, DIR_FORWARD_LEVEL ? HIGH : LOW);
  }
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  Serial.begin(SERIAL_BAUD);
  bootMs = millis();
  lastRampMs = bootMs;
  lastHeartbeatMs = bootMs;

  // USB 연결 시 우노가 리셋되므로 이 줄은 놓칠 수 있습니다. 정보용입니다.
  Serial.println(F("BOOT pwm=0 ready=1"));
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      if (lineOverflow) {
        Serial.println(F("ERR LINE_TOO_LONG"));
      } else if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
      }
      lineLen = 0;
      lineOverflow = false;
      digitalWrite(PIN_LED, LOW);
      continue;
    }
    if (lineLen < LINE_MAX) {
      lineBuf[lineLen++] = c;
    } else {
      lineOverflow = true;
    }
  }

  rampTick();
  watchdogTick();
}
