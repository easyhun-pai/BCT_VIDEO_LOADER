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
review/                 오탐 검수 세션 CLI (P0: 현장 MinIO·Influx 읽기·다운로드)
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
- **저장** `--out` > NAS `\192.168.33.22ct-review`(접근 가능할 때) > `data/review/` 순. 경로 `{site}/{yyyy-mm-dd}/{event_id}/{hook,ppe}.mp4` + `fetch.json`.
- **메모 형식** 그대로: `* 26/08/26` 줄 아래 `0946 7` (HHMM BCT). 매칭 실패·중복은 ±10분 후보와 함께 보고한다.
- 현장 추가 = `sites.json` 항목 하나 + `secrets.local.json` 항목 하나. 현장 방문·현장 변경 없음.
