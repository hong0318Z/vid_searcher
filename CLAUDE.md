# VidSort v5 — Claude Code 시스템 가이드

> 새 세션 시작 시 이 파일을 먼저 읽어서 전체 구조를 파악하세요.

---

## 프로젝트 개요

로컬 영상 파일 관리 데스크탑 앱 (Python + tkinter).  
태그·별칭·설명 관리, JAV 스크래핑, LLM 자동 태그, 웹 갤러리 뷰 포함.

**개발 브랜치**: `claude/add-claude-api-integration-9kZWy`  
**배포 형태**: 미정 (EXE 단일 파일 예정, 아직 빌드 설정 미업데이트)

---

## 파일 구조

```
vid_searcher/
├── vidsort.py        # 메인 앱 (4600줄) — DB, UI, 모든 비즈니스 로직
├── jav_scraper.py    # JAV 메타 스크래퍼 (648줄)
├── llm_api.py        # GitHub Copilot LLM 클라이언트 (248줄)
├── web_gallery.py    # Flask 웹 갤러리 서버 (776줄)
├── VidSort.spec      # PyInstaller 빌드 설정 (미업데이트 — 새 의존성 반영 필요)
├── vidsort.db        # SQLite DB (런타임 생성)
├── vidsort_cfg.json  # 설정 저장 (런타임 생성)
└── .thumbs/          # 썸네일 캐시 (md5해시.jpg)
```

---

## vidsort.py 구조

### 전역 상수
```python
DB_PATH   = _BASE / "vidsort.db"
CFG_PATH  = _BASE / "vidsort_cfg.json"
THUMB_DIR = _BASE / ".thumbs"
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', ...}
PAGE_SIZE  = 500   # 한 화면 최대 영상 수
```

### class DB (line 126)
SQLite 래퍼. 모든 DB 접근은 `threading.Lock`으로 보호.

**주요 테이블**:
```sql
files(
  path TEXT PRIMARY KEY,  -- 전체 경로
  name TEXT,              -- 파일명
  alias TEXT,             -- 별칭 (사용자 지정 제목)
  description TEXT,       -- 설명
  size, duration, width, height, thumb_ok,
  folder TEXT,            -- 최상위 폴더 (추가된 폴더 경로)
  added_at REAL,
  ext TEXT,               -- 소문자 확장자 (.mp4 등)
  jav_done INTEGER,       -- 1 = LLM 처리 완료
  jav_raw TEXT            -- JAV 스크래핑 원본 JSON
)

tags(path TEXT, tag TEXT, PRIMARY KEY(path, tag))

tag_meta(tag TEXT PRIMARY KEY, description TEXT)
```

**주요 메서드**:
- `query_page(active_exts, folder, tag, sort, short_filter, search, offset, limit, min_dur, folder_search)` — 검색/필터/페이징 메인 쿼리
- `rename_tag(old, new)` — INSERT OR IGNORE + DELETE 방식 (UNIQUE 충돌 방지)
- `delete_tag(tag)` — 전체 삭제 (tags + tag_meta)
- `get_jav_done_list(search, limit)` — JAV 관련 파일만 반환 (`jav_raw!=''` OR `jav_done=1 AND alias LIKE '%[%-%]%'`)
- `reset_jav(path)` — jav_done=0, jav_raw='', alias='', description='' + 태그 삭제

### class CanvasGrid (line 616)
Canvas 기반 커스텀 영상 썸네일 그리드. 500+ 영상을 배치 렌더링.

- `on_open` 콜백 → 더블클릭 시 호출 (현재 `_viewer_dlg` 연결)
- `on_ctx` 콜백 → 우클릭 메뉴
- `load()` / `hard_load()` — 소프트/하드 리렌더링

### class VidSort(tk.Tk) (line 981)
메인 윈도우. 주요 메서드 그룹:

**UI 빌드**
- `_build_ui()` — 상단 검색바 + 툴바 + 메인 영역 + 사이드바
- `_build_sidebar()` — 폴더/태그 목록
- `_style()` — TTK 다크 테마

**검색/로드**
- `_reload()` — 검색 조건 수집 → 스레드로 `_bg_query` 실행
- `_bg_query()` — DB 쿼리 실행 → `_on_query_done` 콜백
- `folder_search_var` — 툴바 '폴더명' 체크박스 변수 (기본 False)

**파일 편집 다이얼로그**
- `_alias_dlg(path)` — 별칭 편집
- `_desc_dlg(path)` — 설명 편집
- `_tag_dlg(paths)` — 태그 편집 (다중 파일)
- `_viewer_dlg(path)` — 인라인 뷰어/편집 패널 (더블클릭 연결)
  - Linux X11 + mpv: `--wid=<wid>` 임베딩
  - 그 외: 썸네일 표시 + 클릭 시 외부 재생
  - 이전/다음 탐색 (← → 키)

**태그 관리** (`_tag_manage_dlg`, line 1436)
- 태그 목록 + 설명 편집
- `🗑 태그 삭제` — `db.delete_tag()`
- `🔗 AI 태그 통합 (한국어 정리)` — LLM이 유사 태그 그룹 제안 → 사용자 체크 → 통합 실행
  - 대표 태그 Entry로 직접 편집 가능
  - `check_vars = {원본rep: (BooleanVar, StringVar(편집rep), members)}`

**LLM 기능**
- `_get_llm_client()` — LLMClient 인스턴스 반환 (토큰 없으면 경고)
- `_llm_auto_tag_dlg()` — AI 자동 태그 설정 다이얼로그
- `_llm_run_batch(paths, tag_pool, extra_prompt, add_names)` — 실제 배치 태깅
  - 파일명을 `[폴더명] 파일.mp4` 형식으로 LLM에 전달 (폴더 컨텍스트)
- `_llm_auto_tag_paths(paths)` — 경로 목록 즉시 태깅 (우클릭 메뉴)

**JAV 기능**
- `_jav_process_dlg()` — JAV 처리 메인 다이얼로그 (2탭)
  - Tab1: 파일 목록, `selectmode='extended'` (다중선택), 우클릭 제외
  - Tab2: LLM 처리 완료 목록, 우클릭 초기화
  - LLM 응답 디버그 패널 (토글)
- `_jav_db_dlg()` — JAV DB 뷰어 (검색/태그 번역/초기화)
- `_jav_exclude(paths)` — JAV 처리 대상에서 제외 (태그 추가)
- `_llm_worker()` (내부) — 배치당 LLM 호출, JSON 파싱, 태그/별칭 저장
  - `genres_ko` + `meta.get('genres')` 모두 `_GENRE_MAP` 통과
  - 장르 태그 개수 제한 없음 (배우만 `[:4]`)

**`_GENRE_MAP`** (line ~3794)
영어/일본어 장르 → 한국어 변환 딕셔너리.  
JAV LLM 워커에서 장르 태그를 DB 저장 전에 반드시 통과시킴.

---

## llm_api.py 구조

**설정 상수**
```python
GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL    = "claude-sonnet-4.5"
BATCH_SIZE       = 50
MAX_OUTPUT_TOKENS = 64000   # Sonnet 최대 출력. 모든 호출에 이 값 사용
```

**GitHub Copilot API 필수 헤더 4개** (`_COPILOT_HEADERS`):
```
Editor-Version, Editor-Plugin-Version, Copilot-Integration-Id, Openai-Organization
```

**class LLMClient**
- `_chat(messages, max_tokens)` → str  
- `_chat_tracked(messages, max_tokens, on_chunk)` → (content, tok_in, tok_out)  
  - **스트리밍(SSE) 방식** — 청크 단위 수신, read timeout=60s (전체 대기 X)
- `analyze_and_tag(filenames, tag_pool, on_progress, system_prompt)` — 배치 자동 태그
  - `NEW:새태그명` 형식으로 새 태그 생성 가능
- `analyze_and_name(filenames, on_progress)` — 배치 한글 이름+설명 생성
  - 파일명 stem < 5글자이면 건너뜀
- `_tag_batch()` / `_name_batch()` — 배치 내부 단위 처리

---

## jav_scraper.py 구조

**스크래핑 순서** (`fetch_meta_verbose`):
1. FC2-PPV 코드 → 즉시 fallback 메타 반환 (외부 사이트 미지원)
2. 오프라인 DB (`jav_offline.json`)
3. R18.dev 공식 API
4. JavDB 스크래핑
5. Javbus 스크래핑

**`extract_code(filename)`** — 파일명에서 JAV 코드 추출
- FC2-PPV-XXXXXXX 우선 처리
- 괄호 안 코드 우선
- 표준 `LETTERS-DIGITS` 패턴

**HTTP 엔진** (우선순위):
1. `curl_cffi` — Cloudflare 우회 (설치 시)
2. `httpx` — 일반
3. `urllib` — 내장 폴백

**Javbus 처리**:
- `over18=1; age=1` 쿠키를 `Cookie` 헤더에 직접 주입
- "Age Verification" 감지 시 confirm 링크 따라가서 재시도

---

## web_gallery.py 구조

Flask 웹 갤러리 서버. `start(db_path, thumb_dir, port=8765)` 로 스레드 실행.

**라우트**:
- `GET /` — 홈 (카테고리 그리드 + 태그 그룹 2개×4영상 + 랜덤 추천)
- `GET /tags` — 전체 태그 목록 + **태그 검색** (JS 실시간 필터)
- `GET /tag/<tagname>` — 태그별 영상 목록
- `GET /search?q=` — 제목/파일명 검색
- `GET /video/<vid_id>` — 영상 상세 + HTML5 플레이어
- `GET /stream/<vid_id>` — 영상 스트리밍 (Range request 지원)
- `GET /thumb/<hash>` — 썸네일 서빙
- `GET /open/<vid_id>` — 로컬 기본 앱으로 재생

**`_get_tag_groups(limit_tags=2, vids_per_tag=4)`** — 홈 추천 섹션 (2개 태그, 각 4영상 랜덤)

---

## 주요 패턴 & 주의사항

### LLM 호출
- **항상 `max_tokens=64000`** 사용. 토큰 비용 무관 (GitHub Copilot 호출)
- 스트리밍 방식이라 대용량 응답도 타임아웃 없음
- JSON 파싱 실패 시 정규식으로 부분 복구 (`_llm_worker` 참고)
- Python 3 예외 변수 소멸 문제: `lambda: f(ex)` 대신 `lambda err=ex: f(err)` 사용하거나 리스트에 저장

### 스레드 안전
- DB 쓰기는 모두 `self.lock` 보유
- UI 업데이트는 반드시 `widget.after(0, callback)` 로 메인 스레드에서
- 워커 스레드에서 직접 tk 위젯 접근 금지

### 태그 통합 UI (Canvas 스크롤)
- `cv.itemconfigure(cw, width=e.width)` — `<Configure>` 이벤트의 `e.width` 사용 (`cv.winfo_width()` 불가 — 실현 전 0 반환)
- `selectcolor='#7c6ff7'` 사용 (배경색과 동일하면 체크박스 시각적으로 작동 안 함)

### rename_tag
```python
# UNIQUE 충돌 방지: UPDATE 대신 INSERT OR IGNORE + DELETE
INSERT OR IGNORE INTO tags(path, tag) SELECT path, ? FROM tags WHERE tag=?
DELETE FROM tags WHERE tag=?
```

### JAV 2-phase 처리
- Phase 1: 스크래핑 → `jav_raw` 컬럼에 JSON 저장
- Phase 2: LLM 배치 → `jav_done=1`, `alias`, `description`, 태그 저장
- 장르 태그: `genres_ko` → `_GENRE_MAP` → DB 저장 (개수 제한 없음)
- 배우 태그: `[:4]` 제한 유지

### 더블클릭 동작
- 더블클릭 → `_viewer_dlg()` (편집 패널 열림, 자동재생 없음)
- 썸네일 클릭 or `▶ 외부 앱` 버튼 → 외부 플레이어 실행
- mpv + X11 환경에서만 창 내 임베딩

---

## 설정 파일 (vidsort_cfg.json)
```json
{
  "llm_token": "GitHub PAT",
  "llm_model": "claude-sonnet-4.5",
  "llm_endpoint": "https://api.githubcopilot.com",
  "llm_system_prompt": "...",
  "folders": [...],
  "formats": {...}
}
```

---

## 미완료 / 향후 작업

- **FC2-PPV 전용 스크래퍼**: FC2DB 등 전용 사이트 추가 필요 (현재 fallback만)
- **EXE 빌드**: `VidSort.spec` 미업데이트 — `httpx`, `flask`, `bs4`, `curl_cffi` hidden imports 추가 필요, `web_gallery.py` 등 py 파일을 datas에 추가 필요
- **갤러리 뷰 내 편집**: 웹 갤러리에서 태그/설명/제목 직접 수정 (미구현)
