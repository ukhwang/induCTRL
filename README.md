# induCTRL
for Mic

MIC의 교반 센서 기록과 완료 판정을 휴대폰에서 확인하는 프로젝트입니다.

- [MIC 수집 및 완료 판정](inductrl_stirctl/README_ko.md)에서 원본 `stirctl` 구성, 모의 실험, SQLite 기록 형식을 확인할 수 있습니다.
- [아두이노 PWM 속도 제어](arduino/README.md)에서 우노 R3 배선, 시리얼 프로토콜, 감속·정지와 MIC 쪽 사용법을 확인할 수 있습니다.
- [교반 모니터 웹앱](webapp/README.md)에서 실시간 실험 시뮬레이터, 조회·비교·녹화 재생, Neon 업로더와 웹 수동 기록을 설정할 수 있습니다.

## 실시간 실험 시뮬레이터

물(L)과 용질(g)을 넣고 `실험 시작`을 누르면 교반조 애니메이션과 함께 판정·축동력·에너지가 실시간으로 쌓이고, 끝나면 결과 카드(판정 시각·총 에너지·잔여 불균일 CV·FIXED 대비 절감)가 나옵니다. 제어 모드는 FIXED(기준 운전) / RULE(판정 엔진 정지) / AI(진단·감속 순환) 세 가지입니다.

```sh
npm --prefix webapp ci
node webapp/server.mjs        # Python(stirctl.py) + SQLite 로 실행 · http://localhost:3000
```

Python 실행이 없는 정적 호스팅에서는 같은 화면이 브라우저 엔진(`webapp/public/engine.js`, `sim.py`·`decision.py` 와 같은 물리식·판정 규칙)으로 자동 전환됩니다. Vercel Hobby(0원) 배포는 [웹앱 문서](webapp/README.md#vercel-무료-배포-hobby-0원)를 따르면 되고, 저장소 루트의 `vercel.json` 이 `webapp/public` 을 그대로 올립니다.
