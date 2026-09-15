# BCT Video Loader

엣지노드(6대)에 트리거로 저장된 PPE/Hook 3초 영상과 판정 결과(`result.json`)를
SSH로 내 PC에 모으는 수집 스크립트.

## 구조

```
config/
  nodes.json            노드 인벤토리(IP·BCT·경로)
  secrets.local.json    계정·비밀번호·봇토큰 (gitignore)
  secrets.example.json  secrets.local.json 템플릿
scripts/
  setup_keys.ps1        SSH 키 일괄 설치 (최초 1회)
  pull_videos.ps1       수집기 (증분)
  pull_videos.sh        rsync 버전 (WSL/Git Bash/Linux)
  find_storage.ps1      저장 경로 추적
  trigger_listener.py   텔레그램 트리거 연동
  review.bat            오탐 검수 세션 CLI 실행기
  review_app.bat        오탐 검수 세션 웹 실행기 (localhost:8501)
  perf_app.ps1/.bat     월간 성능 검수 웹 실행기 (localhost:8502)
review/                 오탐 검수 세션 — CLI(P0) + 로컬 웹 app.py(P1) + session.py
                        월간 성능 검수 — perf_app.py + perf.py(표본·채점·집계) + perf_render.py(리포트)
                        webui.py = 두 웹 앱 공통 화면 부품 (로그인·CSS·영상·단축키)
config/sites.json       검수 대상 현장 목록
data/                   수집 결과 (gitignore)
logs/                   실행 로그 (gitignore)
```

## 노드 ↔ BCT

| 노드 | 담당 BCT | 카메라 |
|---|---|---|
| edge-node1 | BCT13 | 2 |
| edge-node2 | BCT11, BCT12 | 4 |
| edge-node3 | BCT9, BCT10 | 4 |
| edge-node4 | BCT7, BCT8 | 4 |
| edge-node5 | BCT5, BCT6 | 4 |
| edge-node6 | BCT4 | 2 |

접속은 ZeroTier IP(`10.113.93.x`)로 한다. IP 테이블의 `172.81.161.x`는 현장 LAN이라
ZeroTier로는 닿지 않는다. 영상은 카메라가 아니라 엣지노드 디스크에 저장되므로 노드에만
접속하면 된다.

## 준비

1. `secrets.example.json` 을 `secrets.local.json` 으로 복사하고 계정·비밀번호·봇토큰 입력.
2. ZeroTier를 켜고 `ssh <user>@10.113.93.74` 가 되는지 확인.
3. SSH 키 설치 (최초 1회, 이후 무인 동작):

   ```powershell
   cd scripts
   .\setup_keys.ps1
   ```

   PuTTY `plink` 이 PATH에 있으면 비밀번호 자동 입력, 없으면 노드마다 한 번씩 입력한다.

## 수집

```powershell
cd scripts
.\pull_videos.ps1                        # 전체 노드, 증분
.\pull_videos.ps1 -Node edge-node5       # 특정 노드
.\pull_videos.ps1 -SinceDays 1           # 최근 1일
.\pull_videos.ps1 -Date 2026-07-22       # 특정 날짜만
.\pull_videos.ps1 -Date 2026-07-22,2026-07-23   # 여러 날짜
.\pull_videos.ps1 -DryRun                # 받을 파일만 미리보기
.\pull_videos.ps1 -PurgeRemote           # 받은 뒤 원격에서 삭제
```

각 노드에 한 번 접속해 대상 파일 목록을 받고, **로컬에 없는 것만** ssh+tar 스트림으로
가져온다(매번 전체 재다운로드 안 함). 저장 위치는 `data/<노드>/<날짜>/<gate>_<시각>/`.

### `-SinceDays` vs `-Date`

- `-SinceDays N` — `find -mtime` 기반. "최근 N일"이라 **특정 하루만 집어낼 수 없다**.
  5일 전 것만 필요해도 그 사이 날짜가 전부 딸려온다.
- `-Date yyyy-MM-dd` — 원격이 `remoteBaseDir/<날짜>/<gate>_<시각>/` 로 날짜 분할돼 있는
  것을 경로로 직접 좁힌다. 지난 날짜를 정확히 다시 받을 때 이걸 쓴다. 쉼표로 여러 날짜 지정 가능.
- 둘을 같이 주면 `-Date` 가 이기고 `-SinceDays` 는 무시된다(WARN 로그).

노드가 죽어 있으면 해당 노드만 `SSH 접속 실패 → 건너뜀` 으로 기록하고 나머지는 계속 받는다.

WSL/Git Bash라면 rsync 버전:

```bash
./pull_videos.sh                   # 전체
./pull_videos.sh edge-node5        # 특정 노드
DRY=1 ./pull_videos.sh
```

## 설정 (`config/nodes.json`)

- `remoteBaseDir` — 엣지노드의 영상 저장 경로. 기본 `/home/paimedialab/mithril_ai_server/results`.
  모르면 `find_storage.ps1` 로 추적.
- `filePatterns` — 받을 확장자. 기본 `mp4`, `jpg`, `json`.
- `excludeDirs` — 제외할 하위 폴더. 기본 `["raw"]` (프레임 시퀀스는 용량이 커서 제외).
- `localBaseDir` — 저장 위치. 절대경로면 그대로, 상대경로면 `scripts` 기준.

## 구글 드라이브로 저장

`localBaseDir` 만 드라이브 폴더로 바꾸면 된다(코드 변경 없음).
[Drive for Desktop](https://www.google.com/drive/download/)을 설치하면 드라이브가 `G:`
같은 로컬 경로로 마운트되고, 그 폴더가 자동 동기화된다.

```json
"localBaseDir": "G:\\내 드라이브\\[영월] BCT 현장 데이터"
```

## 트리거 연동

엣지노드는 트리거 결과를 텔레그램으로 보낸다. PC에서 그 메시지를 받아 BCT 번호로
해당 노드만 즉시 수집한다(엣지노드 수정 불필요).

```powershell
python scripts\trigger_listener.py
```

`secrets.local.json` 의 `telegramBotToken` 과 `nodes.json` 의 `trigger.bctRegex` 를 쓴다.
BCT→노드 매핑은 노드별 `bcts` 에서 자동 생성된다. 상시 실행은 작업 스케줄러에 등록.

## 주기 실행 (트리거 대신)

```powershell
schtasks /create /tn BCT_Pull /sc minute /mo 10 ^
  /tr "powershell -ExecutionPolicy Bypass -File C:\...\scripts\pull_videos.ps1"
```

## 오탐 검수 세션 CLI (`review/`, P0)

현장 코드를 건드리지 않고 **현장 서버 MinIO·InfluxDB를 읽기만** 해서 이벤트를 나열·필터·다운로드한다.
설계는 `data/_design/fp-review-session/` (설계 v2). 검수자 PC에서 실행하는 클라이언트이고 상시 서버는 없다.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# 현장 자격: config/secrets.local.json 의 sites.<code> (secrets.example.json 참고)

scripts\review sites                                   # 등록된 현장, 저장 루트
scripts\review days HANIL --month 2026-09              # MinIO 에 데이터 있는 날짜·건수
scripts\review check-influx HANIL 2026-09-07           # Influx 필드·조인율·시각차 검증
scripts\review list HANIL 2026-09-07 --verdict denied --from 09:00 --to 12:00 --reason 안전고리
scripts\review resolve HANIL memo.txt --out ids.txt    # 텔레그램 메모(HHMM BCT) → 이벤트 ID
scripts\review fetch HANIL --ids-file ids.txt --tg     # 클립 다운로드 (--tg: 박스 영상도)
scripts\review fetch HANIL 2026-09-07 --verdict denied --bct 7 --dry-run
```

- **이벤트 ID** `{SITE}-{bct}-{yyyymmdd_HHMMSS}` = MinIO 키 `{bct}/{ts}/`. hook/ppe 는 스트림.
- **접근** `sites.json` 의 `access`: `direct`(현장서버 ZeroTier IP) 또는 `tunnel`(엣지노드 SSH 포트포워딩으로 LAN IP 우회). 둘 다 읽기 전용.
- **판정·점수** Influx `gate_event` (`allowed`, `hook/helmet/harness_score`). 사유는 Influx 에 없어 `thresholds` 로 유도 (엣지 `decision_engine` 과 동일 규칙).
- **저장** `--out` > NAS `\\192.168.33.22\DEV\2026_retrain_dataset`(접근 가능할 때) > `data/review/` 순. 경로 `{site}/{yyyy-mm-dd}/{hook|ppe}/{yyyymmdd_HHMMSS}_{bct}_{role}.mp4` + `fetch.json`.
- **메모 형식** 그대로: `* 26/08/26` 줄 아래 `0946 7` (HHMM BCT). 매칭 실패·중복은 ±10분 후보와 함께 보고한다.
- 현장 추가 = `sites.json` 항목 하나 + `secrets.local.json` 항목 하나. 현장 방문·현장 변경 없음.

## 기간 출입 통계 리포트 (`review report`)

```powershell
scripts\review report HANIL 2026-09-07 2026-09-13            # → data/_reports/HANIL_20260907_20260913/
scripts\review report HANIL 2026-09-07 2026-09-13 --gap 120  # 재시도 간격 기준 변경 (기본 300초)
```

이벤트를 **시도 세션**으로 묶어 BCT별·일별·시간대별 승인/거부, 첫 시도 성공률, 재시도 회복률, 거부 사유,
반복 세션, 이상 징후(0건 BCT-일, 조인 누락)를 낸다. 출력: `report.pdf`, `report.html`, 차트 PNG,
`events_raw.csv`, `sessions.csv`(이벤트 ID 포함), `per_bct.csv`, `daily.csv`, `hourly.csv`, `summary.json`.

세션 규칙: 같은 BCT에서 **직전 이벤트가 거부**이고 **5분(300초) 안**에 다음 이벤트가 오면 같은 세션(재시도).
승인이 나오면 세션 종료. 결과는 승인이 하나라도 있으면 승인, 없으면 거부(반복해도 1회).
300초는 데이터로 정했다 — 거부 뒤 다음 이벤트 밀도가 승인 뒤(다른 사람) 기준선보다 120초까지 20배 이상,
300초까지 우세, 그 뒤로는 구분되지 않는다. 거부 사유는 Influx에 없어 현장 임계값으로 유도. PDF는 Chrome/Edge가 있을 때만 생성.

## 오탐 검수 세션 웹 (`review/app.py`, P1)

검수자 PC에서 실행하는 로컬 웹(Streamlit). 브라우저는 `localhost:8501`, 상시 서버 없음.

```powershell
.\scripts\review_app.ps1      # PowerShell — 실행 → 브라우저 자동 오픈, Ctrl+C 로 바로 종료
scripts\review_app.bat        # cmd 용 (Ctrl+C 시 "일괄 작업을 끝내시겠습니까?" 가 뜨는 건 cmd 사양)
```

흐름: **현장 선택 → 일자 선택 → 하루치 목록·필터 → 검수 → 오탐 내보내기**

판정은 **정탐 / 오탐** 둘뿐이다 (2026-09-14 '애매' 제거).

- **필터** 시간대 · 판정(승인/거부) · 사유 · BCT · Hook 점수 구간 · 클래스 미검출(점수 0) · 상태(미검수/검수됨/오탐만)
- **보기 모드** 사이드바에서 전환.
  - **4개씩 (2×2, 기본)** 이벤트 4개 × 카메라 2대 = 영상 8개가 한 화면에. 각 타일에 번호와 "오탐으로 표시" 체크박스.
    키보드 `1`~`4` 체크 토글 · `P` 전부 정탐 · `N` 체크한 것 오탐(나머지 정탐) · `Z` 되돌리기(4개 묶음 단위).
    **오탐 항목**: 체크한 타일에 `1 안전모 · 2 하네스 · 3 안전고리` 칩이 생긴다. 타일 번호를 누르면 그 타일이 파란 테두리로
    활성화되고(체크 안 돼 있었으면 체크도), 이어서 누른 `1`~`3` 이 그 타일의 오탐 항목 토글이 된 뒤 다시 타일 선택으로 돌아온다.
    `Backspace` = 활성 타일 체크 해제 · `Esc` = 고르지 않고 빠짐. 한 타일에 항목을 더하려면 번호 → 항목을 한 번 더.
    예: `3` `3` `1` `2` `N` → 3번 안전고리 오탐, 1번 하네스 오탐, 나머지 정탐.
    이미 오탐으로 저장된 타일은 체크된 채 열리고 저장된 항목이 칩에 들어가 있다. 되돌리기는 이전 판정으로 복원한다(재판정해도 안전).
  - **1개씩** hook·ppe 나란히 + 오탐 항목 칩 + 한 줄 메모. `←` 정탐 `→` 오탐 `Space` 건너뛰기 `Z` 되돌리기 · `1`~`3` 오탐 항목 토글.
  - 오탐 항목은 `session.json` 판정의 `classes`(`helmet`/`harness`/`hook`)에 남고, 하루치 목록에 "오탐 항목" 칸으로 보인다.
  - **예전 오탐 분류** (2026-09-15 이전 판정엔 항목이 없다): 일자 표의 "항목 미분류" 칸으로 날짜를 찾고, 필터 `상태 → 항목 미분류`.
    타일에 그 이벤트의 ❌ 클래스가 제안값으로 들어가 있으니 확인·수정 후 `N`. 저장한 묶음은 목록에서 빠지고 다음 묶음이 온다.
- **모델** 상단 알약에 현장 탐지 모델(예: `PPE 260827ppe2 · Hook 260828hook`). 엣지노드 SSH 로 `compose.yml` 의 모델 파일 해시와
  체크포인트의 학습 이름을 읽어 `{저장루트}/_config/models/{SITE}.json` 에 저장하고, 6시간마다 뒤에서 갱신한다(SSH 키 없는 PC 는 저장된 값만).
  수동 갱신: `scripts\review models HANIL`.
- **영상** `_tg/`(박스 있는 640) 우선, 없으면 학습용. 자동·반복 재생, 기본 2배속(사이드바에서 조절). 브라우저가 못 여는 코덱이면 ffmpeg 로 H.264 변환(로컬 캐시 `data/review/_cache/`).
- **판정 저장** 누를 때마다 `{저장루트}/{site}/{date}/session.json` 에 기록. 중간에 꺼도 마지막 판정 다음부터 이어서. SQLite 아님(SMB 동시쓰기 잠금 회피).
  그리드에서 판정한 묶음은 `history` 에 리스트 하나로 들어가 되돌리기가 묶음 단위다.
- **영상 내보내기** 사이드바 버튼 = 이 날짜를 NAS 에 반영. 오탐 이벤트의 학습용(박스 없음) mp4를 **오탐 항목에 맞는 카메라만** 받는다.
  안전고리 → `hook.mp4`, 안전모·하네스 → `ppe.mp4`, 둘 다면 둘 다, 항목 미분류면 둘 다(분류하면 정리됨).
  필요 없어진 영상(항목이 바뀌었거나 정탐으로 바뀐 이벤트)은 지운다. 판정 기록에 없는 폴더는 건드리지 않는다.
- **☁️ 검수 자료 전량 업데이트** 사이드바 맨 위 버튼. 모든 현장·일자의 검수 기록을 훑어 NAS 에 반영 안 된 것을 한 번에 받고 정리한다.
  CLI 도 같다: `scripts\review sync --dry-run -v` 로 미리 보기, `scripts\review sync` 로 실행.
  ```
  {저장루트}/{site}/{date}/session.json      정탐·오탐 판정 전부 (오탐 항목·검수자·IP·시각·메모), exported = NAS 에 있는 영상
  {저장루트}/{site}/{date}/hook/20260907_042915_bct13_hook.mp4    안전고리 오탐 영상
  {저장루트}/{site}/{date}/ppe/20260907_132613_bct9_ppe.mp4       안전모·하네스 오탐 영상
  ```
  같은 날짜 안에서는 이벤트별 폴더를 만들지 않고 카메라 폴더에 모은다(파일명 = 트리거 시각 + BCT + 카메라).
  예전 구조(`{date}/{event_id}/hook.mp4`)로 받아 둔 영상은 반영할 때 새 구조로 옮기고 빈 폴더는 지운다(다시 받지 않음).
- **저장 루트** `BCT_REVIEW_OUT` 환경변수 > NAS `\\192.168.33.22\DEV\2026_retrain_dataset` (안 붙어 있으면 `secrets.local.json` 의 `nas` 자격으로 `net use` 한 번 시도) > `data/review/`.
- **로그인** 첫 화면에서 ID/비밀번호. 계정은 `config/users.local.json`(gitignore)에 PBKDF2 해시로 저장.
  ```powershell
  scripts\review user add admin        # 비밀번호 프롬프트
  scripts\review user list
  ```
  로그인 ID가 곧 검수자라 세션 파일의 `by` 에 남는다. 페이지를 새로고침하면 다시 로그인한다.
  공통 계정 하나(`admin`)를 쓰므로 **접속 IP**로 사람을 구분한다: 로그인마다 `{저장루트}/_logins.jsonl` 한 줄,
  세션 파일 `reviewers` 목록, 판정 한 건마다 `ip`. localhost 접속이면 `127.0.0.1`.

헤드리스 테스트: `streamlit.testing.v1.AppTest` 로 현장→일자→검수→판정→되돌리기→내보내기 흐름을 실제 현장 데이터에 대해 돌려 확인했다(2026-09-09, 514건).

## 월간 성능 검수 웹 (`review/perf_app.py`)

매달 모델 성능 리포트용. BCT 당 100건(기본)을 무작위로 뽑아 사람이 채점하고, 정확도·오탐율을 낸다. 브라우저는 `localhost:8502` (오탐 검수 :8501 과 동시 실행 가능).

```powershell
.\scripts\perf_app.ps1        # PowerShell
scripts\perf_app.bat          # cmd
```

흐름: **현장 선택 → 월 선택 (처음이면 표본 만들기) → 채점 → 리포트**

- **표본 만들기** 그 달 이벤트 중 판정 연동·세 클래스 점수·영상이 모두 있는 것이 후보. BCT 별로 시드 고정 셔플해 앞에서 N건(후보가 적으면 전부).
  한 달치 수집은 수십 초(9월 1~14일 9,510건 12초). 모델 버전을 적어 두면 리포트·비교에 쓰인다.
- **채점 (2×2)** 타일마다 `안전모 · 하네스 · 안전고리 · 제외` 칩. **모델 판정이 틀린 항목만** 누르고 `P`(저장 · 다음 4개), `Z` 되돌리기(묶음 단위).
  아무것도 안 누르면 세 항목 모두 맞음. `제외` = 판단 불가(사람 안 보임·영상 깨짐) → 집계에서 빠지고 같은 BCT 의 다음 무작위 후보가 대기열 끝에 자동 보충.
  타일 색: 미검수 하늘색 · 미검수 거부 노랑 · 틀림 표시 빨강 · 제외 회색 · 저장된 것은 결과색.
- **리포트** 사이드바 `화면 → 리포트`. 출입 판정 정확도(Wilson 95% CI) · 전 항목 정답률 · 부당 거부/승인 · BCT 별 · 클래스 별 오탐율/미탐율 · 오류 사례 ·
  **모델 개선 비교**(다른 달 결과를 기준으로 지표별 %p 변화). `리포트 만들기` → HTML·PDF·CSV (출입 통계 리포트와 같은 양식), 화면에서 PDF/HTML 받기.
- **정의** 모델 판정: 점수 ≥ 현장 임계값이면 착용(⭕). 실제 = 틀림 표시한 항목만 뒤집음. 실제 출입 = 세 항목 모두 실제 착용이면 승인.
  오탐율 = 미착용(❌) 판정 중 실제 착용 비율, 미탐율 = 착용(⭕) 판정 중 실제 미착용 비율. 부당 거부 = 시스템 거부·실제 승인, 부당 승인 = 그 반대.
- **저장** (NAS 저장 루트, 오탐 검수와 분리)
  ```
  {저장루트}/_perf/{site}/{yyyy-mm}/pool.json     후보 전체 + 시드 (한 번 만들고 고정 → 표본 재현 가능)
  {저장루트}/_perf/{site}/{yyyy-mm}/review.json   대기열 · 채점(누가·IP·시각) · 되돌리기 이력
  {저장루트}/_perf/{site}/{yyyy-mm}/report/       report.html · report.pdf · graded.csv · per_bct.csv · per_class.csv · errors.csv · summary.json
  ```
  여러 명이 같은 달을 채점해도 저장 직전에 최신 파일을 다시 읽어 합친다. 로그인 계정은 오탐 검수와 같다.

`review/webui.py` 는 두 웹 앱이 같이 쓰는 화면 부품이다. 이 파일(및 `review/` 의 앱 스크립트가 아닌 모듈)을 고치면 Streamlit 서버를 다시 켜야 반영된다.
