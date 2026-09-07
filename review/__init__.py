"""BCT 오탐 검수 세션 — P0: 현장 MinIO/Influx 읽기 전용 카탈로그·다운로드 CLI.

구조는 data/_design/fp-review-session/ 설계 v2 를 따른다.
- 검수자 PC 에서 실행하는 클라이언트. 상시 서버 없음.
- 현장에는 읽기만 한다 (MinIO 나열·다운로드, Influx 조회).
- 결과는 NAS 공유 폴더(없으면 로컬 폴더)에 {site}/{yyyy-mm-dd}/{event_id}/ 로 쌓는다.
"""

__version__ = "0.1.0"
