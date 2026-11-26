# induCTRL 교반 모니터

휴대폰에서 MIC 배치의 전도도, 상·하부 탁도, 파생 RPM, 완료 후보 및 정지 확인을 조회합니다. 비교와 녹화 재생은 원본 SQLite에 기록된 `elapsed_s`를 기준으로 표시합니다. 미검증 완료 후보, 정지 미확인, 중단은 각각 구분합니다. `OK` 측정값만 숫자로 표시하며, 0도 유효할 수 있습니다.

## 원본 stirctl 모의 데이터로 실행

Node.js 24가 필요합니다. 저장소에 포함된 원본 stirctl로 아래 명령을 실행합니다. 원본 Python 파일 수정이나 장비 연결 없이 생성한 SQLite를 웹앱에서 읽습니다.

```sh
# 저장소의 inductrl_stirctl 디렉터리
python3 stirctl.py run --mode mock --material saltwater --batch-id saltwater-01 --fast --db /tmp/stirctl-demo.sqlite
python3 stirctl.py run --mode mock --material caco3 --batch-id caco3-01 --fast --db /tmp/stirctl-demo.sqlite
```

```sh
# 이 저장소 루트
npm --prefix webapp ci
STIR_SQLITE_DB=/tmp/stirctl-demo.sqlite node webapp/server.mjs
```

기본 포트는 3000입니다. Codex에서 휴대폰으로 확인하려면 환경의 **HTTP Previews**에 3000번 포트를 추가합니다. 서버 실행 후, 다른 터미널에서 `--fast`를 생략하고 새 배치 ID로 다시 실행하면 기다리는 중 → 완료 후보 → 정지 확인이 실시간으로 바뀝니다. DB 없이 `node webapp/server.mjs --fast`를 실행하면 화면 조작용 내장 모의 데이터도 볼 수 있습니다.

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
