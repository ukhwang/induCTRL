# induCTRL 교반 모니터 · 실시간 시뮬레이터 · 전시장 원격 제어

휴대폰에서 MIC 배치의 전도도, 상·하부 탁도, 파생 RPM, 완료 후보 및 정지 확인을 조회합니다. 비교와 녹화 재생은 원본 SQLite에 기록된 `elapsed_s`를 기준으로 표시합니다. 미검증 완료 후보, 정지 미확인, 중단은 각각 구분합니다. `OK` 측정값만 숫자로 표시하며, 0도 유효할 수 있습니다.

화면 위쪽의 **실시간 실험** 패널에서 실험 종류(소금물·탄산칼슘)와 물(L)·용질(g)·제어 모드(FIXED/RULE/AI)·재생 속도를 정하고 `실험 시작`을 누르면, 교반조 애니메이션과 함께 판정·에너지가 실시간으로 쌓이고 끝나면 결과 카드가 나옵니다.

그 위의 **LIVE / 전시장 교반기** 패널은 실제 설비를 움직입니다. 방문객이 자기 폰으로 `indu-ctrl.vercel.app` 에 접속해 `교반 시작`을 누르면 **Vercel → Neon DB → 현장 MIC → 아두이노 PWM → 모터** 순서로 실제 교반이 시작되고, 센서 값은 같은 폰 화면에 바로 올라옵니다.

## 전체 구조

```
[방문객·목업 폰]  https://indu-ctrl.vercel.app
        │  POST /api/command  (START/STOP)          GET /api/snapshot, /api/command/status
        ▼
[Vercel 서버리스 함수]  webapp/api/*.mjs
        │  INSERT stir_commands (PENDING)           SELECT batches/readings/events, mic_devices
        ▼
[Neon PostgreSQL]  스키마 stir_monitor  ← 전시장에서 인터넷만 되면 어디서든 같은 주소
        ▲  UPDATE … PENDING → RUNNING (1초 폴링, FOR UPDATE SKIP LOCKED)
        │  INSERT mic_devices (5초 생존 신호)       ▲ upload.py 가 SQLite → Neon (2초)
[MIC 에이전트]  webapp/mic/agent.py
        │  아두이노 PWM(SET_PCT) + stirctl.py run --mode observe
        ▼
[모터·탱크·센서]  ─ stirctl 이 SQLite 에 기록 ─→ upload.py
```

핵심은 **명령은 DB 를 거쳐 가고, 데이터도 DB 를 거쳐 온다**는 점입니다. 폰과 MIC 가 같은 와이파이에 있을 필요가 없습니다.

## 실행 경로 세 가지

| 경로 | 언제 | 무엇이 도는가 | 제어 |
|---|---|---|---|
| **전시(Neon)** | Vercel + Neon + MIC | 서버리스 API + MIC 에이전트 + 실물 모터 | 폰에서 실제 교반 |
| **로컬 서버** | `node webapp/server.mjs` | `stirctl.py run --mode mock` 자식 프로세스 + SQLite | 폰/브라우저에서 모의 교반 |
| **브라우저 엔진** | 정적 배포, Python 없음 | `public/engine.js` — `sim.py`·`decision.py` 와 같은 물리식·판정 규칙 | 화면 안에서만 |

Neon 환경변수가 없으면 `/api/command` 와 `/api/snapshot` 은 `{ mode: "sim_only" }` 를 돌려주고, 화면은 LIVE 패널을 숨긴 뒤 위쪽 시뮬레이터만 그대로 씁니다. **환경변수를 하나도 넣지 않은 기존 배포도 100% 그대로 동작합니다.**

---

# 1. 전시 준비 (한 번만 하면 됨)

## 1-1. Neon DB 만들기 (0원)

1. [neon.tech](https://neon.tech) 가입 → **New Project**. 리전은 **Singapore(ap-southeast-1)** 또는 **Tokyo** 중 가까운 곳. 프로젝트를 만들면 `neondb` 데이터베이스와 접속 문자열이 나옵니다.
2. 왼쪽 **SQL Editor** 에서 [neon/schema.sql](neon/schema.sql) 전체를 붙여넣고 실행합니다. `stir_monitor` 스키마와 테이블(`batches`, `readings`, `events`, `decision_log`, `manual_entries`, `upload_health`, `web_manual_entries`, **`stir_commands`**, **`mic_devices`**)이 만들어집니다. 여러 번 실행해도 안전합니다.
3. 같은 편집기에서 [neon/roles.sql](neon/roles.sql) 을 실행합니다. 권한 역할 5개가 만들어집니다.
4. **접속 계정 2개**를 만듭니다. 비밀번호는 각자 정하고 안전한 곳에 적어 둡니다.

   ```sql
   -- MIC(현장)용: 업로더 + 명령 처리
   CREATE ROLE stir_mic LOGIN PASSWORD 'MIC에서_쓸_비밀번호';
   GRANT stir_upload, stir_mic_agent TO stir_mic;

   -- Vercel(폰)용: 조회 + 명령 적재
   CREATE ROLE inductrl_web LOGIN PASSWORD '웹에서_쓸_비밀번호';
   GRANT stir_web_command TO inductrl_web;
   ```

5. 대시보드 **Connect** 에서 접속 문자열을 복사합니다. **MIC 는 Direct connection**, **Vercel 은 Pooled connection**(호스트에 `-pooler` 가 붙은 것)을 씁니다.

   | 위치 | 환경변수 | 값 예시 |
   |---|---|---|
   | MIC | `NEON_UPLOAD_URL` | `postgresql://stir_mic:비번@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt` |
   | Vercel | `DATABASE_URL` | `postgresql://inductrl_web:비번@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require` |
   | 양쪽 | `MIC_SOURCE_ID` | `mic-01` (기본값. 바꾸면 **양쪽에 같은 값**을 넣어야 함) |

   > `sslmode=verify-full` 이 인증서 오류를 내면 `sslmode=require` 로 바꿔도 됩니다(암호화는 유지, 호스트 검증만 생략).

## 1-2. Vercel 설정 (환경변수 1~2개)

서버리스 함수는 프로젝트의 **Root Directory 아래 `api/`** 에서만 찾습니다. 이 저장소는 함수가 `webapp/api/` 에 있으므로 **Root Directory 가 `webapp`** 이어야 합니다. (이미 그렇게 배포되어 있으면 그대로 두면 됩니다 — `https://indu-ctrl.vercel.app/api/command/status` 가 200 JSON 을 돌려주면 정상입니다.)

1. Vercel 프로젝트 → **Settings → General → Root Directory** 가 `webapp` 인지 확인. 비어 있거나 저장소 루트라면 `webapp` 으로 바꾸고 Redeploy 합니다.
2. **Settings → Environment Variables** 에 추가:

   | 이름 | 값 | 필수 |
   |---|---|---|
   | `DATABASE_URL` | 1-1 의 **Pooled** 접속 문자열 | 필수 |
   | `MIC_SOURCE_ID` | `mic-01` | 선택(기본값과 같으면 생략) |
   | `EXHIBITION_PIN` | 4~12자리 숫자 (예: `2468`) | 선택 — 넣으면 시작/정지에 PIN 이 필요합니다 |

3. **Deployments → Redeploy**. 환경변수를 새로 넣으면 기존 배포에는 자동 반영되지 않으므로 한 번 다시 배포해야 합니다. (Root Directory 를 바꿨다면 그것도 함께 반영됩니다.)
4. 확인: 브라우저에서 `https://indu-ctrl.vercel.app/api/command/status` 를 열어

   ```json
   { "mode": "neon", "mic": { "online": false, ... }, "active": null, ... }
   ```

   가 나오면 성공입니다. `{"mode":"sim_only", ...}` 이면 아직 `DATABASE_URL` 이 없거나 재배포하지 않은 것입니다. `404` 면 Root Directory 가 `webapp` 이 아닙니다.

> **PIN 을 켤까?** 전시장에서 방문객이 직접 눌러 보게 하려면 **비워 두세요**. 운영자가 있을 때만 눌리게 하려면 `EXHIBITION_PIN` 을 넣고 그 숫자를 부스에 적어 둡니다. PIN 을 켜면 폰 화면에 입력칸이 생깁니다.

## 1-3. MIC 설정 (현장 Jetson)

```sh
# 1) 코드 받기 (한 번만)
git clone https://github.com/ukhwang/induCTRL /opt/inductrl
cd /opt/inductrl
python3 -m venv .venv
.venv/bin/pip install -r webapp/mic/requirements.txt

# 2) 환경변수 (관리자만 읽을 수 있게)
sudo tee /etc/stir-agent.env > /dev/null <<'EOF'
NEON_UPLOAD_URL=postgresql://stir_mic:비번@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt
MIC_SOURCE_ID=mic-01
EOF
sudo chmod 600 /etc/stir-agent.env

# 3) 아두이노 포트 확인
ls /dev/ttyACM*        # 보통 /dev/ttyACM0
```

### 실행 (이 한 줄이면 업로더까지 같이 돕니다)

```sh
set -a; . /etc/stir-agent.env; set +a
cd /opt/inductrl
.venv/bin/python webapp/mic/agent.py \
  --db /opt/stirctl/data/inductrl_readings.sqlite3 \
  --config /opt/stirctl/config.json \
  --stirctl-dir /opt/inductrl/inductrl_stirctl \
  --mode observe \
  --pwm-port /dev/ttyACM0 \
  --with-upload --upload-state /var/lib/stir-upload/checkpoint.json
```

- `--with-upload` 는 `mic/upload.py` 를 자식으로 띄워 SQLite → Neon 업로드를 같이 돌립니다. 업로더를 따로 돌리고 있다면 빼세요.
- 상시 운영은 [mic/stir-agent.service](mic/stir-agent.service) 를 `/etc/systemd/system/` 에 복사하고 경로만 고친 뒤 `sudo systemctl enable --now stir-agent` 입니다. `Restart=always` 라서 죽으면 다시 뜹니다.
- 테스트는 `curl -s https://indu-ctrl.vercel.app/api/command/status | head -c 200` 로 `"online": true` 를 확인합니다.

### `--mode` 선택

| 모드 | 하는 일 | 언제 |
|---|---|---|
| `observe` (기본) | Modbus 로 센서를 읽고 판정·기록만. **정지 출력 없음** | 아두이노 PWM 으로 모터를 돌리는 전시 기본값 |
| `live` | 위 + Modbus 정지 코일 쓰기. `--arm-stop` 도 함께 줘야 동작 | 정지 릴레이까지 검증된 설비 |
| `mock` | 하드웨어 없이 가상 센서로 리허설 | 전시 전 연습, 노트북 데모 |

`observe` 에서는 stirctl 이 모터를 직접 세우지 않습니다. **자동 정지 판정이 끝나면 에이전트가 PWM 을 0 으로 내립니다**(교반 종료·비상 정지·에이전트 종료 모두). 그래서 `stir-agent` 프로세스가 죽어도 모터는 서지만, **물리 비상정지 버튼은 따로 두는 것을 권합니다.**

### PWM 없이 쓰기

- 아두이노를 안 쓸 때: `--no-pwm`
- 포트 없이 대화만 흉내: `--pwm-simulate`
- 교반 세기: 폰에서 고른 `교반 세기(%)` 가 그대로 `SET_PCT` 로 전달됩니다(10~100%).

## 1-4. 전시 전 리허설 (하드웨어 없이)

```sh
# 터미널 1 — 로컬 서버 + 내장 시뮬레이터
npm --prefix webapp ci
node webapp/server.mjs          # http://localhost:3000

# 터미널 2 — 에이전트를 하드웨어 없이 (PWM 대화만 흉내)
NEON_UPLOAD_URL='...' MIC_SOURCE_ID=mic-01 \
  python3 webapp/mic/agent.py --db /tmp/rehearsal.sqlite3 --mode mock --pwm-simulate \
  --with-upload --upload-state /tmp/rehearsal-upload.json
```

폰에서 사이트를 열고 `교반 시작` → `비상 정지` 를 눌러 보면 명령·MIC 상태·배치가 어떻게 바뀌는지 확인할 수 있습니다.

## 1-5. 전시 당일 체크리스트

- [ ] MIC 전원·랜선·아두이노 USB 연결, `ls /dev/ttyACM*` 확인
- [ ] `systemctl status stir-agent` → `active (running)`
- [ ] `curl -s https://indu-ctrl.vercel.app/api/command/status` → `"online": true`
- [ ] 모터 주전원 투입 전에 폰에서 `교반 시작` → 모터가 **천천히** 도는지 눈으로 확인 → `비상 정지`
- [ ] 목업 폰: 사이트 접속 → 화면 위 LIVE 패널이 **MIC 온라인** 으로 보이는지
- [ ] QR 코드가 `https://indu-ctrl.vercel.app` 를 가리키는지 (해시 없이도 LIVE 패널이 홈에 나옵니다)
- [ ] 방문객 2명이 동시에 눌러도 한 명만 시작되고 다른 폰에는 **현재 운전 중 (잠금)** 이 뜨는지 확인

## 1-6. 자주 나는 문제

| 증상 | 원인 / 조치 |
|---|---|
| LIVE 패널이 안 보임 | `/api/command/status` 가 `sim_only` → Vercel Root Directory 가 `webapp` 인지, `DATABASE_URL` 이 있는지 확인 후 Redeploy |
| `MIC 오프라인` | 에이전트가 꺼졌거나 `MIC_SOURCE_ID` 가 웹과 다름. `systemctl status stir-agent`, `/etc/stir-agent.env` 확인 |
| 시작을 눌러도 `409` | 다른 방문객이 운전 중이거나, 이전 명령이 남아 있음. `비상 정지` 를 한 번 누르면 정리됩니다 |
| 시작을 눌러도 모터가 안 돎 | `--pwm-port` 확인, `ls /dev/ttyACM*`, 에이전트 로그의 `PWM …% 적용` / `PWM 속도 설정 실패` 확인 |
| 센서 값이 안 올라옴 | `--with-upload` 유무, 업로더 체크포인트 경로 권한, `/api/snapshot` 의 `received_at` 확인 |
| Vercel 빌드 실패 | Root Directory 를 `webapp` 으로 두면 `webapp/package.json` 이 적용됩니다. Output Directory 는 `webapp/vercel.json` 의 `public` |

---

# 2. 로컬 실행 (Python + SQLite)

Node.js 24와 python3가 필요합니다. 환경 변수를 주지 않으면 시뮬레이터용 SQLite 를 `/tmp/inductrl-sim.sqlite` 에 자동으로 만듭니다.

```sh
npm --prefix webapp ci
node webapp/server.mjs            # http://localhost:3000 · 시뮬레이터 + 로컬 제어 큐
```

MIC 원본 기록을 함께 보려면 기존처럼 DB 경로를 지정합니다.

```sh
# 저장소의 inductrl_stirctl 디렉터리
python3 stirctl.py run --mode mock --material saltwater --batch-id saltwater-01 --fast --db /tmp/stirctl-demo.sqlite
# 이 저장소 루트
STIR_SQLITE_DB=/tmp/stirctl-demo.sqlite node webapp/server.mjs
```

DB 없이 화면만 둘러보려면 `node webapp/server.mjs --demo`(내장 모의 데이터).

로컬 서버에서는 `POST /api/command` 가 **같은 서버의 시뮬레이터**를 MIC 처럼 다룹니다. 즉 Neon 없이도 폰 화면의 LIVE 패널을 그대로 눌러 볼 수 있습니다(시뮬레이터 = `stirctl.py run --mode mock`).

## API

| 메서드 · 경로 | 내용 |
|---|---|
| `GET /api/snapshot`, `GET /api/live` | 현재 배치·샘플·이벤트·아카이브. DB 미설정 시 `{mode:"sim_only"}` |
| `GET /api/batch?id=…` | 배치 하나의 전체 기록 |
| `POST /api/command` | `{action:"START", material, water_l, solute_g, control_mode, target_rpm_pct}` 또는 `{action:"STOP"}` → `202` 접수 / `409` 운전 중 / `503` MIC 오프라인 / `401` PIN / `429` PIN 반복 실패 |
| `GET /api/command/status` | MIC 온라인 여부(`mic.online`), 진행 중 명령(`active`), 대기 중 정지(`stop`), 최근 5개 명령, 최근 배치 |
| `POST /api/sim/start` · `/api/sim/stop` · `GET /api/sim/status` | 로컬 서버의 모의 실험(기존 경로) |
| `POST /api/manual` (선택) | 웹 수동 기록(PIN 필요) |

```sh
# 폰 대신 터미널에서 눌러 보기
curl -s localhost:3000/api/command/status
curl -X POST localhost:3000/api/command -H 'Content-Type: application/json' \
  -d '{"action":"START","material":"saltwater","water_l":10,"solute_g":500,"control_mode":"rule","target_rpm_pct":60}'
curl -X POST localhost:3000/api/command -H 'Content-Type: application/json' -d '{"action":"STOP"}'
```

## 안전 규칙 (명령 큐)

- **한 번에 하나만**: `stir_commands` 의 부분 유니크 인덱스가 기기당 진행 중 `START` 를 1개로 제한합니다. 방문객 2명이 동시에 눌러도 한 명만 202, 다른 한 명은 409 입니다.
- **MIC 오프라인이면 시작 불가**: 마지막 생존 신호가 `MIC_ONLINE_SECONDS`(기본 20초)보다 오래되면 `503 mic_offline`. 폰에서도 버튼이 잠깁니다.
- **정지는 항상 접수**: MIC 가 꺼져 있어도 정지 명령은 큐에 남고, 재접속하면 처리됩니다.
- **응답 없는 시작 자동 취소**: 60초 넘게 `PENDING` 인 시작 명령은 다음 시작 요청 때 `CANCELED` 로 정리합니다.
- **에이전트 재시작 정리**: 켜질 때 이전 `RUNNING` 은 `FAILED`, 2분 넘은 `PENDING` 은 `CANCELED` 로 남겨 두고 다시 실행하지 않습니다.
- **모터 우선 정지**: STOP·자동 종료·에이전트 종료(SIGTERM) 모두 PWM 을 먼저 0 으로 내립니다.

---

# 3. Vercel 배포 상세

- **Root Directory**: `webapp` (권장). 그러면 `webapp/vercel.json` 이 적용되고, `public/` 정적 파일과 `api/` 서버리스 함수가 함께 배포됩니다.
- **Output Directory**: `public` — `webapp/vercel.json` 에 지정됨. Build Command 없음, Framework **Other**.
- **리전**: `icn1`(서울) — 폰과 가까워 폴링 지연이 줄어듭니다.
- 무료 Hobby 플랜은 함수 호출·전송량에 월 한도가 있고, 한도를 넘으면 **자동 과금 없이** 다음 달까지 제한됩니다. 전시 하루(방문객 100명 × 2초 폴링 ≈ 20~30만 회)는 한도 안에서 충분합니다.
- `DATABASE_URL` 이 없으면 함수는 200 + `{mode:"sim_only"}` 를 돌려주고 화면은 시뮬레이터로 폴백합니다. 정적 배포만 하던 기존 설정을 그대로 둬도 사이트는 깨지지 않습니다.

> Root Directory 가 저장소 루트인 배포(저장소 루트 `vercel.json` 의 `outputDirectory: webapp/public`)는 정적 전용입니다. 그 경우 화면은 그대로 뜨지만 `/api/*` 가 404 라 LIVE 패널이 숨겨지고 시뮬레이터만 동작합니다. LIVE 기능을 쓰려면 Root Directory 를 `webapp` 으로 바꿔야 합니다.

---

# 4. 테스트

```sh
npm --prefix webapp test                                   # Node 25개 (엔진·프로세스·조회·명령 큐·API 폴백)
cd inductrl_stirctl && python3 -m unittest discover -s tests -t .   # Python 154개
cd webapp/mic && python3 -m unittest discover -s tests -t .         # 에이전트 14개
```

`webapp/test/engine.test.mjs` 는 브라우저 엔진과 `stirctl.py` 를 같은 조건으로 돌려 **판정 시각·에너지가 같은 범위**에 있는지 대조합니다(python3 가 없으면 건너뜀). `webapp/test/command.test.mjs` 는 명령 큐의 중복 차단·오프라인 차단·PIN·자동 취소를 가짜 DB 로 확인합니다.

---

# 5. Neon 연결 상세 (이미 스키마를 만든 경우)

`schema.sql` 은 `IF NOT EXISTS` 라서 다시 실행해도 기존 데이터가 지워지지 않습니다. 전시 기능만 나중에 추가하려면 `stir_commands`·`mic_devices`·인덱스 부분만 실행해도 됩니다.

| 접속 용도 | 부여할 역할 | 환경 변수 |
| --- | --- | --- |
| MIC 업로더 | `stir_upload` | `NEON_UPLOAD_URL` |
| MIC 명령 처리(에이전트) | `stir_mic_agent` | 위와 같은 `NEON_UPLOAD_URL` |
| 웹 조회 + 명령 적재(Vercel) | `stir_web_command` | `DATABASE_URL` |
| 선택 사항인 웹 수동 기록 | `stir_web_write` | `NEON_WEB_WRITE_URL` |

`stir_web_command` 는 `stir_commands` 에 INSERT/UPDATE(상태 열만) 와 SELECT, IDENTITY 시퀀스 USAGE 만 가집니다. 그 외 테이블은 읽기만 가능합니다.

### MIC 업로더 단독 실행 (에이전트 없이)

```sh
.venv/bin/python mic/upload.py --db /opt/stirctl/data/inductrl_readings.sqlite3 \
  --state /var/lib/stir-upload/checkpoint.json --once
```

`--once`는 대기 중인 데이터를 전송하고 종료합니다. 상시 실행은 [systemd 서비스 예시](mic/stir-upload.service)를 사용합니다. 업로더는 2초마다 로컬 변경을 확인하고, 성공한 PostgreSQL 트랜잭션 이후에만 체크포인트를 전진시킵니다. 배치 상태, 센서값, 이벤트, 판정 로그, MIC 수동 기록을 동기화하며 원시 레지스터·장치 설정은 제외합니다.

웹 입력은 `web_manual_entries`에만 추가합니다. 결과에는 `MIC`과 `웹` 출처가 표시됩니다. 비교와 재생 화면은 최근 완료 배치 8개를 제공합니다.
