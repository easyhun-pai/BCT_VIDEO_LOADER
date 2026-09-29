# BCT Video Loader

한일시멘트 BCT 출입 통제(PPE·안전고리 판정) 현장의 영상과 판정 결과를 **읽기 전용**으로 가져와
수집 · 오탐 검수 · 월간 성능 검수 · 보고서를 만드는 검수자 PC 용 도구. 상시 서버는 없다.
사용 설명은 [README.md](README.md), 이 파일은 작업할 때 지켜야 할 구조와 규칙이다.

## 현장

| 코드 | 현장 | BCT | 서버 (ZeroTier) | 구성 |
|---|---|---|---|---|
| `HANIL` | 한일시멘트 영월 | bct4 ~ bct13 (10개소) | 10.113.93.41 | 엣지노드 6대 + 현장 서버 분리 |
| `DANYANG` | 한일시멘트 단양 | bct6, bct7 (2개소) | 10.113.93.185 | 엣지 앱 · MinIO · Influx · 대시보드가 한 장비 |

- 접속은 ZeroTier IP 로만. 현장 LAN IP(`172.81.161.x`, `172.19.5.x`)는 닿지 않는다.
- 현장 서버: MinIO `:9000` (버킷 `bct-clips`), InfluxDB `:8086` (org `mithril`, 버킷 `gate_events`).
- 판정 임계값은 두 현장 동일: 안전모 0.45 · 하네스 0.50 · 안전고리 0.50.
- 현장 추가 = `config/sites.json` 항목 하나 + `config/secrets.local.json` 의 `sites.<CODE>` 하나.

## 기능 단위

### 1. 영상 수집 (`scripts/`)

엣지노드 디스크의 트리거 영상 · `result.json` 을 SSH 로 증분 수집한다. 영월 전용.

| 파일 | 역할 |
|---|---|
| `scripts/pull_videos.ps1` · `.sh` | 수집기 (증분, `-Date` · `-SinceDays` · `-DryRun`) |
| `scripts/setup_keys.ps1` | SSH 키 일괄 설치 (최초 1회) |
| `scripts/trigger_listener.py` | 텔레그램 트리거 → 해당 노드만 즉시 수집 |
| `scripts/find_storage.ps1` | 엣지노드 저장 경로 추적 |
| `config/nodes.json` | 노드 인벤토리 (IP · BCT · 경로) |

### 2. 현장 접근 · 이벤트 카탈로그 (`review/` 공통)

모든 검수 기능의 바탕. 현장은 읽기만 한다.

| 파일 | 역할 |
|---|---|
| `review/config.py` | `sites.json` + `secrets.local.json` 로드, 저장 루트 결정 |
| `review/access.py` | `direct`(ZeroTier IP) 또는 `tunnel`(SSH 포트포워딩) |
| `review/catalog.py` | MinIO 이벤트 나열. 이벤트 ID = `{SITE}-{bct}-{yyyymmdd_HHMMSS}` |
| `review/influx.py` | `gate_event` 조회, 이벤트와 조인(트리거 후 90초 안), 사유 유도 |
| `review/download.py` · `memo.py` | 클립 다운로드, 텔레그램 메모 → 이벤트 ID |
| `review/auth.py` | 웹 로그인 계정 (PBKDF2, `users.local.json`) |
| `review/models.py` | 엣지노드 탐지 모델 정보 (SSH 키 필요) |

클립 키: 학습용 `{bct}/{ts}/{hook|ppe}.mp4` (1280, 박스 없음), 검수용 `_tg/{bct}/{ts}/{hook|ppe}.mp4` (640, 박스 있음).

### 3. 오탐 검수 웹 (`:8501`)

하루치 이벤트를 전부 보며 정탐/오탐을 판정하고, 오탐 영상을 NAS 로 내보낸다.

| 파일 | 역할 |
|---|---|
| `review/app.py` | Streamlit 앱 (현장 → 일자 → 검수 → 내보내기) |
| `review/session.py` | 판정 저장 `{저장루트}/{site}/{date}/session.json` |
| `review/export.py` | 오탐 항목에 맞는 카메라만 NAS 반영 (안전고리 → hook, 안전모·하네스 → ppe) |
| `scripts/review_app.ps1` · `.bat` | 실행기 |

### 4. 월간 성능 검수 웹 (`:8502`)

BCT 당 100건 표본을 사람이 채점해 정확도 · 오탐율을 낸다.

| 파일 | 역할 |
|---|---|
| `review/perf_app.py` | Streamlit 앱 (현장 → 월 → 2×2 채점 → 리포트) |
| `review/perf.py` | 표본 추출 · 채점 저장(`PerfStore`) · 집계(`summarize`) |
| `review/perf_render.py` | 현장 단위 리포트 (차트 · HTML · PDF · CSV) |
| `scripts/perf_app.ps1` · `.bat` | 실행기 |

**표본 규칙 (층화)** — `perf.stratified_order`
- BCT 마다 N건을 시스템 승인/거부 반반. 한쪽이 모자라면 그쪽 전부 + 나머지를 다른 쪽에서.
- 승인 · 거부 각각 안에서 일자 → 시간대(2시간) 라운드로빈. 건수가 많은 날도 같은 몫.
- `pool.json` 의 BCT 별 순서 = [승인 표본][거부 표본][보충 순서]. 시드로 재현.
- `제외`(판단 불가)는 같은 BCT · 같은 판정의 다음 후보로 자동 보충.

**채점 규칙**
- 검수자는 모델 판정이 **틀린 항목만** 표시한다. 표시 안 한 항목은 모델이 맞은 것.
- 실제 출입 = 세 항목 모두 실제 착용 · 체결이면 승인.

**용어 (경보 기준, 프로젝트 전체 공통 — 바꾸지 말 것)**
- 오탐 = 모델 미착용(X) · 실제 착용(O) → 부당 거부의 원인
- 미탐 = 모델 착용(O) · 실제 미착용(X) → 부당 승인의 원인
- 출입 판정 정확도 = 시스템 승인/거부가 실제와 같은 비율 (Wilson 95% CI)

### 5. 월간 통합 보고서 · 추가 학습 목록

여러 현장의 채점 결과를 PDF 한 부로 묶는다.

| 파일 | 역할 |
|---|---|
| `review/perf_combined.py` | 통합 리포트(`build`), 추가 학습 클립 목록(`build_retrain_list`) |

보고서 구성: 1장 검수 환경 · 기준 + 종합 정확도(통합 · 현장별) → 통합 상세 → 현장별 상세 → 부록(검수 이벤트 전체 목록).

**보고서 작성 규칙**
- 본문은 사실과 수치만. 생성 시각, 검수자, 시드, 가중 설명, 제외 목록은 싣지 않는다.
- 1장은 검수 환경 · 기준과 종합 정확도로 끝나고, 상세는 2장부터 시작한다(`.pb` 페이지 나눔).
- 표는 페이지에 걸쳐 이어지되 행은 자르지 않는다.
- 제외 표본과 채점 메타는 `graded_all.csv` · `summary.json` 에만 남긴다.

### 6. 기간 출입 통계 리포트

| 파일 | 역할 |
|---|---|
| `review/report.py` · `report_render.py` | 시도 세션 집계(재시도 간격 300초), BCT · 일별 · 시간대 통계, PDF |

`report_render.py` 의 `CSS` · `html_to_pdf` · `_mpl` 은 모든 리포트가 같이 쓴다.

### 7. 공통 웹 부품 (`review/webui.py`)

두 웹 앱이 같이 쓰는 로그인 · 사이드바(홈 버튼) · CSS · 영상 받기 · 단축키 스크립트.

## 월간 검수 · 보고서 절차

매월 초, 지난 달을 대상으로 한다. `<YYYY-MM>` 만 바꾸면 된다.

```powershell
# 1. 표본 만들기 (모든 현장, BCT 당 100건 층화 추출)
scripts\review perf make all 2026-09
scripts\review perf show all 2026-09          # 승인/거부 · 날짜 · 시간대 분포 확인

# 2. 채점 (웹 :8502, 현장마다 월을 열어 끝까지)
.\scripts\perf_app.ps1

# 3. 통합 보고서 PDF
scripts\review perf combined all 2026-09      # → data/_reports/perf_2026-09/report.pdf

# 4. 추가 학습용 오탐 · 미탐 클립 목록
scripts\review perf retrain all 2026-09       # → retrain_clips.json, retrain_event_ids.txt

# 5. (사무실, NAS 연결 상태) 클립 받기
scripts\review fetch HANIL   --ids-file data\_reports\perf_2026-09\retrain_event_ids.txt --roles hook,ppe
scripts\review fetch DANYANG --ids-file data\_reports\perf_2026-09\retrain_event_ids.txt --roles hook,ppe
```

채점 단축키: 타일에서 틀린 항목 칩(안전모 · 하네스 · 안전고리 · 제외)을 누르고 `P` 저장, `Z` 되돌리기.

## 저장 위치

| 경로 | 내용 | git |
|---|---|---|
| `config/sites.json` · `nodes.json` | 현장 · 노드 정의 | 추적 |
| `config/secrets.local.json` · `users.local.json` | 자격 · 계정 | 무시 |
| `data/review/{site}/{date}/` | 오탐 검수 판정 · 내보낸 영상 | 무시 |
| `data/review/_perf/{site}/{yyyy-mm}/` | `pool.json` · `review.json` · `report/` | 무시 |
| `data/review/_cache/` | 재생용 영상 캐시 (항상 로컬) | 무시 |
| `data/_reports/perf_{yyyy-mm}/` | 통합 보고서 · 추가 학습 목록 | 무시 |

저장 루트 우선순위: `BCT_REVIEW_OUT` 환경변수 > NAS `\\192.168.33.22\DEV\2026_retrain_dataset` > `data/review/`.
NAS 는 사무실 LAN 에서만 닿는다. 밖에서 채점한 기록은 로컬에 있으니 사무실에서 합칠 때 주의한다.

## 개발 규칙

**환경**
- Windows 11, Python 3.11, `.venv`. 설치: `python -m venv .venv; .venv\Scripts\pip install -r requirements.txt`.
- `pandas` 는 3 미만으로 고정. 3.x 는 Windows Smart App Control 이 `.pyd` 를 차단한다.
- PDF 는 Chrome 또는 Edge 가 있어야 만들어진다. 영상 변환은 ffmpeg.

**현장 · 자격**
- 현장 서버는 읽기 전용. 쓰기 · 삭제 · 설정 변경을 하지 않는다.
- 자격(`secrets.local.json`, `users.local.json`, SSH 키)은 커밋하지 않는다. 출력에도 값을 찍지 않는다.
- 현장 장비의 `compose.yml` 등 운영 설정은 사용자가 직접 읽어 값을 전달한다.

**코드**
- 웹 앱 스크립트(`app.py`, `perf_app.py`)는 매 실행마다 다시 읽힌다. `review/` 의 다른 모듈을 고치면 **서버 재시작**이 필요하다.
- 렌더 경로에서 NAS(`\\192.168.33.22`)를 직접 확인하지 않는다. `os.path.isdir` 한 번이 20초 걸린다.
  저장 루트는 `Settings.resolve_out_root()` 의 기억된 결과를 쓴다.
- 버튼은 `on_click` 콜백으로 처리한다. 분기 안 `st.rerun()` 은 화면을 두 번 그린다.
- 판정 저장은 JSON 원자적 쓰기(`_atomic_write`). SQLite 를 쓰지 않는다(SMB 동시쓰기 잠금).
- Git Bash heredoc 으로 파일을 쓸 때 `\\r` · `\\n` 이 실제 제어문자로 바뀐다. 백슬래시가 든 코드는 Write/Edit 도구로 쓴다.
- 파일은 CRLF 와 LF 가 섞여 있다. 줄바꿈만 바뀐 변경은 커밋하지 않는다.

**검증**
- 웹 흐름: `streamlit.testing.v1.AppTest` 에 `session_state` (`user` 는 `id` · `name` · `ip`)를 넣어 헤드리스로 확인.
- 실제 채점 기록을 건드리지 않도록 `BCT_REVIEW_OUT` 을 임시 폴더 사본으로 돌린다.
- 현장 연결: `scripts\review days <SITE> --month <YYYY-MM>`, `scripts\review check-influx <SITE> <DATE>`.

## CLI 요약

```
sites · days · list · check-influx · resolve · fetch      이벤트 조회 · 다운로드
report SITE FROM TO                                       기간 출입 통계 리포트
models SITE · sync [SITE] · user add|list|del             모델 정보 · NAS 반영 · 계정
perf make|show SITE|all MONTH                             월간 표본 만들기 · 분포 확인
perf report SITE MONTH                                    현장 단위 성능 리포트
perf combined all MONTH                                   통합 보고서 PDF
perf retrain all MONTH                                    추가 학습용 오탐 · 미탐 클립 목록
```

실행은 `scripts\review <명령>` 또는 `.venv\Scripts\python.exe -m review <명령>`.
