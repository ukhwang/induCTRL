# induCTRL 교반 모니터 · 실시간 시뮬레이터

휴대폰에서 MIC 배치의 전도도, 상·하부 탁도, 파생 RPM, 완료 후보 및 정지 확인을 조회합니다. 비교와 녹화 재생은 원본 SQLite에 기록된 `elapsed_s`를 기준으로 표시합니다. 미검증 완료 후보, 정지 미확인, 중단은 각각 구분합니다. `OK` 측정값만 숫자로 표시하며, 0도 유효할 수 있습니다.

화면 위쪽의 **실시간 실험** 패널에서 실험 종류(소금물·탄산칼슘)와 물(L)·용질(g)·제어 모드(FIXED/RULE/AI)·재생 속도를 정하고 `실험 시작`을 누르면, 교반조 애니메이션과 함께 판정·에너지가 실시간으로 쌓이고 끝나면 결과 카드가 나옵니다.

## 실행 경로 두 가지

| 경로 | 언제 | 무엇이 도는가 | 결과 저장 |
|---|---|---|---|
| **서버(로컬·MIC)** | `node webapp/server.mjs` 로 직접 실행 | `stirctl.py run --mode mock` 자식 프로세스 + SQLite | `STIR_SQLITE_DB`(없으면 `/tmp/inductrl-sim.sqlite`) |
| **브라우저 엔진** | 정적 호스팅(Vercel), 또는 Python·SQLite 가 없는 환경 | `public/engine.js` — `sim.py`·`decision.py` 와 같은 물리식·판정 규칙 | 브라우저 메모리(새로고침 시 사라짐) |

화면은 `/api/sim/start` 를 먼저 시도하고, 응답이 `python_unavailable`/`sim_disabled` 이거나 API 자체가 없으면(정적 배포) 자동으로 브라우저 엔진으로 넘어갑니다. 그래서 같은 화면이 로컬에서도 온라인에서도 100% 동작합니다.

## 로컬 실행 (Python + SQLite)

Node.js 24와 python3가 필요합니다. 환경 변수를 주지 않으면 시뮬레이터용 SQLite 를 `/tmp/inductrl-sim.sqlite` 에 자동으로 만듭니다.

```sh
npm --prefix webapp ci
node webapp/server.mjs            # http://localhost:3000 · 시뮬레이터 사용 가능
```

MIC 원본 기록을 함께 보려면 기존처럼 DB 경로를 지정합니다.

```sh
# 저장소의 inductrl_stirctl 디렉터리
python3 stirctl.py run --mode mock --material saltwater --batch-id saltwater-01 --fast --db /tmp/stirctl-demo.sqlite
# 이 저장소 루트
STIR_SQLITE_DB=/tmp/stirctl-demo.sqlite node webapp/server.mjs
```

DB 없이 화면만 둘러보려면 `node webapp/server.mjs --demo`(내장 모의 데이터).

### 시뮬레이터 API

| 메서드 · 경로 | 내용 |
|---|---|
| `POST /api/sim/start` | `{material, water_l, solute_g, control_mode, speed_factor}` → 실행 중인 자식 프로세스를 먼저 종료하고 새로 시작, `{ok, batch_id}` 반환 |
| `POST /api/sim/stop` | 실행 중인 시뮬레이션을 SIGINT 로 끝내고 배치를 `INTERRUPTED` 로 마감 |
| `GET /api/sim/status` | 실행 여부, 현재 조건, 종료 코드, 최근 로그, 배치 요약 |
| `GET /api/snapshot`, `GET /api/batch?id=…` | 기존 조회 API. 응답에 `sim`(조건·판정 시점 결과·history)이 추가됩니다 |

```sh
curl -X POST localhost:3000/api/sim/start -H 'Content-Type: application/json' \
  -d '{"material":"saltwater","water_l":10,"solute_g":500,"control_mode":"ai","speed_factor":10}'
curl localhost:3000/api/sim/status
```

## Vercel 무료 배포 (Hobby, 0원)

정적 파일만 올리므로 서버·DB·과금이 없습니다. 슬립도 트래픽 과금도 없고, 컴퓨터를 꺼도 그대로 동작합니다(실행은 방문자 브라우저의 엔진이 담당).

1. [vercel.com/new](https://vercel.com/new) → **Import Git Repository** → `ukhwang/induCTRL` 선택 (GitHub 계정 연동 그대로).
2. 설정은 그대로 두면 됩니다: Framework **Other**, Root Directory **저장소 루트**, Build Command 없음, Output Directory `webapp/public`(`vercel.json` 에 지정됨).
3. **Deploy** → `https://<프로젝트>.vercel.app` 접속 → `실험 시작`을 누르면 브라우저 엔진으로 실험이 돌고 결과 카드가 나옵니다.
4. 이후 `main` 에 푸시할 때마다 자동 재배포됩니다.

Root Directory 를 `webapp` 으로 지정하는 방식도 됩니다(그때는 `webapp/vercel.json` 이 `public` 을 배포합니다). CLI 로 배포하려면 `npx vercel --prod`(로그인 필요).

> 정적 배포에는 조회 API 가 없으므로 `실시간`·`결과 비교`·`녹화 재생` 탭은 MIC 기록을 보여주지 않습니다(안내 문구 표시). 실험·결과 카드는 시뮬레이터 패널에서 그대로 동작합니다.

## 테스트

```sh
npm --prefix webapp test                                   # Node 14개 (엔진·시뮬레이터 프로세스·조회)
cd inductrl_stirctl && python3 -m unittest discover -s tests   # Python 83개 (기존 69 + 시뮬레이터 14)
```

`webapp/test/engine.test.mjs` 는 브라우저 엔진과 `stirctl.py` 를 같은 조건으로 돌려 **판정 시각·에너지가 같은 범위**에 있는지 대조합니다(python3 가 없으면 건너뜀).

## Neon 연결

1. Neon 대상 데이터베이스에서 관리자 접속으로 [schema.sql](neon/schema.sql), [roles.sql](neon/roles.sql)을 순서대로 적용합니다. 스키마는 `stir_monitor`입니다.
2. 접속 계정은 SQL에서 `CREATE ROLE ... LOGIN`으로 각각 생성하고, 아래 권한 역할 하나씩 부여합니다. `psql`의 `\password <계정>`이나 보안 비밀 관리 UI로 암호를 설정합니다. 기본 관리자 권한이 붙은 계정을 애플리케이션에 사용하면 역할 분리 효과가 사라집니다.

   | 접속 용도 | 부여할 역할 | 환경 변수 |
   | --- | --- | --- |
   | MIC 업로더 | `stir_upload` | `NEON_UPLOAD_URL` |
   | 웹 조회 | `stir_web_read` | `NEON_READ_URL` |
   | 선택 사항인 웹 수동 기록 | `stir_web_write` | `NEON_WEB_WRITE_URL` |

3. 세 연결 문자열은 대상 데이터베이스의 **직접 연결 주소**를 사용하고 인증서 검증을 활성화합니다. Node 연결은 `sslmode=verify-full`, MIC의 libpq 연결은 `sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt`를 사용합니다. 환경 변수 값은 각 서버의 비밀 설정에 보관합니다.
4. 업로더와 웹 서버에 같은 `MIC_SOURCE_ID`를 지정합니다. 원본 SQLite마다 다른 ID를 사용합니다. 웹 서버에서 `STIR_SQLITE_DB` 대신 `NEON_READ_URL`을 주입하고 `node webapp/server.mjs`를 실행합니다.
5. PIN 입력을 켜려면 쓰기 계정 연결 문자열과 6~12자리 `WEB_ENTRY_PIN`, 웹 페이지의 정확한 HTTPS Origin인 `WEB_ORIGIN`을 서버에 설정합니다. 화면의 수동 입력에서 시료·품질·정지 시각을 기록할 수 있습니다. PIN은 입력에만 적용되므로 조회 화면은 인증된 프리뷰나 접근 제어가 있는 HTTPS 경로에 제공하세요.

### MIC 업로더 실행

```sh
cd /opt/inductrl-webapp  # 이 저장소의 webapp 디렉터리를 배치한 경로 예시
python3 -m venv .venv
.venv/bin/pip install -r mic/requirements.txt
.venv/bin/python mic/upload.py \
  --db /opt/stirctl/data/inductrl_readings.sqlite3 \
  --state /var/lib/stir-upload/checkpoint.json --once
```

`--once`는 대기 중인 데이터를 전송하고 종료합니다. 상시 실행은 이를 생략하거나 [systemd 서비스 예시](mic/stir-upload.service)를 사용합니다. 서비스의 전용 계정이 원본 SQLite와 활성 WAL/SHM 파일을 읽을 수 있고 상태 디렉터리에 쓸 수 있도록 설정합니다. 유닛의 경로를 현장에 맞게 바꾼 뒤 `NEON_UPLOAD_URL`, `MIC_SOURCE_ID`를 관리자만 읽을 수 있는 `/etc/stir-upload.env`에 넣고 유닛을 활성화합니다.

업로더는 2초마다 로컬 변경을 확인합니다. 성공한 PostgreSQL 트랜잭션 이후에만 ID별 체크포인트를 전진시키므로 재접속 시 이어서 전송합니다. 할 일이 없으면 10분 상태 신호가 필요할 때만 접속합니다. 원본 DB나 대상 DB를 바꿀 때는 별도 상태 파일을 사용합니다. 배치 상태, 처리된 센서값, 이벤트, 판정 로그, MIC 수동 기록을 동기화합니다. 원시 레지스터·장치 설정·기존 `sensor_raw`는 동기화 대상에서 제외합니다.

웹 입력은 `web_manual_entries`에만 추가합니다. 결과에는 `MIC`과 `웹` 출처가 표시됩니다. 설비에서 나온 완료·정지 정보는 MIC 기록에서 읽습니다. 비교와 재생 화면은 최근 완료 배치 8개를 제공합니다.
