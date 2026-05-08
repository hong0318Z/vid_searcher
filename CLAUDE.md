# VidSort — Claude Code 시스템 가이드

> 새 세션 시작 시 이 파일을 먼저 읽어서 전체 구조를 파악하세요.

---

## 프로젝트 개요

로컬 영상 파일 관리 데스크탑 앱 (Python + tkinter).  
태그·별칭·설명 관리, 웹 자동태그(FC2 스크래핑+LLM), AI 추천 검색, AI 외부 검색,
VLC 인라인 플레이어, 웹 갤러리 뷰, 스트리밍 다운로더 포함.

**현재 작업 브랜치**: `claude/review-optimization-strategy-BXW2b`  
**배포 형태**: EXE 단일 파일 예정 (VidSort.spec 미업데이트 — 빌드 설정 갱신 필요)

---

## 파일 구조

```
vid_searcher/
├── vidsort.py               # 메인 앱 (~7160줄) — DB, UI, 모든 비즈니스 로직
├── jav_scraper.py           # FC2/JAV 메타 스크래퍼 + 외부 키워드 검색
├── llm_api.py               # GitHub Copilot LLM 클라이언트
├── web_gallery.py           # Flask 웹 갤러리 서버
├── downloader/
│   ├── downloader.py        # 스트리밍 다운로더 (yt-dlp 기반, ~1150줄)
│   ├── downloader_cfg.json  # 다운로더 설정 (런타임 생성)
│   └── requirements.txt
├── _test_fc2ppvdb.py        # fc2ppvdb.com API 테스트 (개발용)
├── VidSort.spec             # PyInstaller 빌드 설정 (미업데이트)
├── vidsort.db               # SQLite DB (런타임 생성)
├── vidsort_cfg.json         # 설정 저장 (런타임 생성)
└── .thumbs/                 # 썸네일 캐시 (md5해시.jpg)
```

---

## vidsort.py 구조

### 전역 상수 / 초기화
```python
_BASE     = Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
DB_PATH   = _BASE / "vidsort.db"
CFG_PATH  = _BASE / "vidsort_cfg.json"
THUMB_DIR = _BASE / ".thumbs"
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', ...}
PAGE_SIZE  = 500   # 한 화면 최대 영상 수
HAS_VLC    = True/False  # python-vlc 설치 여부
```

**EXE 경로 규칙**: 모든 런타임 파일 경로는 반드시 `_BASE /`를 사용.  
`Path(__file__).parent`는 frozen EXE에서 임시 `_MEIPASS` 디렉터리를 가리키므로 사용 금지.

### VLC 초기화 (`_setup_vlc_path()`)
모듈 상단에서 Windows 레지스트리/Program Files에서 libvlc.dll 경로를 찾아
`os.environ['PYTHON_VLC_LIB_PATH']`에 설정. `HAS_VLC` 플래그로 가용 여부 관리.

### class DB (line ~181)
SQLite 래퍼. 모든 DB 접근은 `threading.Lock`으로 보호.

**주요 테이블**:
```sql
files(
  path TEXT PRIMARY KEY,
  name TEXT, alias TEXT, description TEXT,
  size, duration, width, height, thumb_ok,
  folder TEXT,       -- 최상위 폴더 경로
  added_at REAL,
  ext TEXT,          -- 소문자 확장자 (.mp4 등)
  jav_done INTEGER,  -- 1 = LLM 처리 완료
  jav_raw TEXT       -- 스크래핑 원본 JSON
)
tags(path TEXT, tag TEXT, PRIMARY KEY(path, tag))
tag_meta(tag TEXT PRIMARY KEY, description TEXT)
```

**주요 메서드**:
- `query_page(..., sort_asc=None, only_missing_thumb=False)` — 검색/필터/페이징
  - `only_missing_thumb=True` → `WHERE f.thumb_ok = 0` 조건 추가
- `rename_tag(old, new)` — INSERT OR IGNORE + DELETE 방식 (병합 안전)
- `delete_tag(tag)` — tags + tag_meta 전체 삭제
- `get_jav_done_list(search, limit)` — `jav_raw!=''` OR `jav_done=1 AND alias LIKE '%[%-%]%'`
- `reset_jav(path)` — jav_done=0, jav_raw='', alias='', description='' + 태그 삭제
- `get_fc2_fallback_list(limit)` — source='fc2-fallback'인 항목 반환
- `update_jav_raw(path, raw_json)` — jav_raw만 업데이트 (alias/태그 유지)
- `recommend_search(tags, keywords, limit)` — AI 추천용 검색 (150개 랜덤 후 50개 반환)

### class CanvasGrid (line ~891)
Canvas 기반 커스텀 영상 썸네일 그리드. 500+ 영상 배치 렌더링.

- `on_open` → 더블클릭 시 `_viewer_dlg` 연결
- `on_ctx` → 우클릭 메뉴
- `load()` / `hard_load()` — 소프트/하드 리렌더링
- **GC 안전**: `_draw_card()`에서 cache hit 시에도 반드시 `self._phs[path] = image` 저장
  (저장 안 하면 Python GC가 이미지 수집 → 캔버스 빈칸)

### class VidSort(tk.Tk) (line ~1256)
메인 윈도우. 주요 메서드 그룹:

**UI 빌드**
- `_build_ui()` — 상단 검색바 + 툴바 + 메인 영역(PanedWindow) + 사이드바
  - 메인 영역은 `tk.PanedWindow(orient='horizontal')` — 사이드바 너비 드래그 조절 가능 (min 180px, 기본 265px)
- `_build_sidebar()` — 접이식 섹션 구조 (폴더 / 검색필터 / 포맷필터 / AI 도구 / 태그)
  - `_make_collapsible(parent, title, initially_open)` — 토글 헬퍼
    - **주의**: `toggle()` 내에서 `body.pack(fill='x', padx=0, after=hdr)` 필수
      (`after=hdr` 없으면 섹션이 맨 아래에 붙음)
  - `_add_scroll()` / `_fwd_scroll()` — 태그 버튼 휠 이벤트를 `_tag_canvas`로 전달
- `_style()` — TTK 다크 테마

**검색/정렬**
- `_reload()` — 검색 조건 수집 → `_only_missing_thumb` 읽고 초기화(1회 사용) → 스레드로 `_bg_query` 실행
- `sort_asc_var` — `tk.BooleanVar` (기본 True). 툴바 ▲/▼ 버튼으로 토글
- `_only_missing_thumb` — 썸네일 미생성 필터 1회 플래그. `_reload()` 호출 전에 `True`로 설정

**파일 편집 다이얼로그**
- `_alias_dlg(path)` — 별칭 편집
- `_desc_dlg(path)` — 설명 편집
- `_tag_dlg(paths)` — 태그 편집 (다중 파일)
- `_viewer_dlg(path)` — 인라인 뷰어/편집 패널 (더블클릭)
  - `HAS_VLC=True`: VLC 인라인 재생 + 재생/일시정지/정지/시크바/볼륨
  - `HAS_VLC=False` or 실패: 썸네일 표시 + 클릭 시 외부 재생
  - ← → 키로 이전/다음 탐색

**컨텍스트 메뉴** (`_ctx`)
- 뷰어/편집, 재생, 탐색기 열기, 별칭/설명/태그 편집, AI 자동 태그
- `🔄 태그 초기화` → `_reset_tags_dlg(paths)` — alias/태그/jav_done/jav_raw 전체 초기화
- `🚫 웹 자동태그 제외` → `_jav_exclude(paths)`
- DB에서 제거

**폴더 현황** (`_show_folder_overview`, line ~5173)
- 썸네일 그리드로 폴더별 대표 이미지 표시
- 더블클릭 → 해당 폴더만 필터링해서 메인 그리드 표시
- 우클릭 컨텍스트 메뉴:
  - 폴더 필터 적용
  - 썸네일 미생성 영상만 보기 (`_only_missing_thumb = True` → `_reload()`)
  - 썸네일 일괄 생성
  - 폴더 탐색기 열기

**태그 관리** (`_tag_manage_dlg`)
- 왼쪽: 태그 목록 리스트박스
- 오른쪽: 이름 변경(병합 포함) / 설명 저장 / 태그 삭제
- LLM 태그 번역 (일본어→한국어), LLM 태그 통합 (유사 태그 병합)

**AI 추천 검색** (`_ai_recommend_dlg`)
- 자연어 입력 → 1차 LLM (`recommend_query`) → 로컬 DB 태그+키워드 추출
- `recommend_search` → 150개 후보 랜덤 셔플 → 50개
- 2차 LLM (`recommend_explain`) → 스트리밍 점원 설명
- 결과를 메인 썸네일 그리드에 로드 가능

**AI 외부 검색** (`_ai_external_search_dlg`)
- 자연어 입력 → 1차 LLM (`external_search_query`) → JavDB/FC2DB 검색 키워드 추출 (10개 이내)
- `jav_scraper.search_external()` → 각 키워드로 JavDB + FC2DB 실제 스크래핑
- 2차 LLM (`external_search_explain`) → 스트리밍 점원 설명
- 결과 Treeview, 더블클릭/버튼으로 브라우저에서 해당 URL 열기
- **디버그 로그 패널** (📋 로그 보기 버튼): HTTP 상태, 파싱 카드 수, Cloudflare 차단 여부, HTML 덤프, traceback

**웹 자동태그 기능**
- `_jav_process_dlg()` — 메인 다이얼로그 (2탭: 스크래핑 / LLM 번역)
- `_jav_db_dlg()` — 웹태그 DB 뷰어 (FC2 재스크래핑 / 태그 번역 / 초기화)
- `_jav_exclude(paths)` — 대상에서 제외 (jav_done=1)
- `_reset_tags_dlg(paths)` — 태그/메타 전체 초기화
- `_llm_worker()` (내부) — 배치 LLM 호출 → `_GENRE_MAP` 통과 → DB 저장

**`_GENRE_MAP`** (line ~4100대)
영어/일본어 장르 → 한국어 변환 딕셔너리. JAV LLM 워커에서 필수 통과.

**설정 다이얼로그** (`_llm_settings_dlg`)
- GitHub Copilot 토큰/모델/엔드포인트
- FC2PPVDB 이메일/비밀번호
- 태그 분류 시스템 프롬프트 편집

**다운로더 실행** (`_open_downloader`)
```python
if getattr(sys, 'frozen', False):
    exe = _BASE / 'downloader' / 'downloader.exe'   # EXE 빌드 모드
else:
    script = _BASE / 'downloader' / 'downloader.py'  # 개발 모드
```

---

## llm_api.py 구조

**설정 상수**
```python
GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL     = "claude-sonnet-4.5"
BATCH_SIZE        = 50
MAX_OUTPUT_TOKENS = 64000
```

**class LLMClient**
- `_chat(messages, max_tokens)` → str
- `_chat_tracked(messages, max_tokens, on_chunk)` → (content, tok_in, tok_out) — SSE 스트리밍
- `analyze_and_tag(filenames, tag_pool, on_progress, custom_prompt)` — 배치 자동 태그
- `analyze_and_name(filenames, on_progress)` — 한글 이름+설명 생성
- `recommend_query(user_query, tag_pool, on_chunk)` → `{"tags":[...], "keywords":[...], "intent":"..."}`
- `recommend_explain(user_query, videos, on_chunk)` → str (로컬 DB 결과 점원 설명)
- `external_search_query(user_query, on_chunk)` → `{"searches":[...], "intent":"..."}`
  - 외부 사이트 검색용 키워드 목록 추출 (JavDB/FC2DB에 직접 입력할 키워드)
- `external_search_explain(user_query, results, on_chunk)` → str (외부 검색 결과 점원 설명)
- `classify_tags(tag_list)` → `{"태그명": "인물"|"행위"|"레이블"|"기타"}`
- `analyze_actor_names(names)` → JAV/서양 분류 + 슬러그 추출
- `generate_actor_info_batch(actor_data)` → 한국어 배우 프로필

---

## jav_scraper.py 구조

### 코드 추출 (`extract_code`)
파일명에서 AV/FC2 코드 추출. **아래 패턴은 스킵(None 반환)**:
- `YYYY-MM-DD` 또는 `YYYY_MM_DD` 날짜 패턴 포함
- ` | ` 또는 `｜` 파이프 구분자 포함 (예: `TITLE | 2019-08-03 16_01_55_...`)
- `HH_MM_SS` 시간 패턴 포함

### 스크래핑 순서 (`fetch_meta_verbose`)
```
FC2-PPV 코드:
  0-a. fc2db.net 스크래핑 (로그인 불필요)
  0-b. fc2ppvdb.com API (로그인 필요, 계정 설정 시)
  0-c. fallback 최소 메타 (FC2/FC2-PPV/아마추어 태그)

일반 AV 코드:
  1. 오프라인 JSON (jav_offline.json)
  2. R18.dev 공식 API
  3. JavDB 스크래핑
  4. Javbus 스크래핑
```

### 키워드 검색 (외부 AI 검색용)
- `search_javdb(keyword, max_results, on_log)` — `https://javdb.com/search?q=<keyword>&f=all`
  - 카드: `.movie-list .item, .search-video-section .item`
  - Cloudflare 차단 시 카드 0개 + 페이지 title 로그
- `search_fc2db(keyword, max_results, on_log)` — `https://fc2db.net/?s=<keyword>`
  - 카드: `article.post, .work-list .item, .entry, .post`
- `search_external(keyword, max_per_site, on_log)` — JavDB + FC2DB 합산
- `on_log` 콜백: HTTP 상태, 파싱 카드 수, Cloudflare 차단 여부, HTML 덤프, traceback 전달

### HTTP 엔진 (우선순위)
1. `curl_cffi` (impersonate='chrome110') — Cloudflare 우회
2. `httpx` — 일반
3. `urllib` — 내장 폴백

### FC2 전용 스크래퍼
- `_fetch_fc2db(code)` — HTML < 5KB → 소프트 404 감지
- `_fetch_fc2ppvdb(code)` — `isLoggedIn == 0` 감지 시 자동 재로그인 (1회 재귀)

---

## downloader/downloader.py 구조

yt-dlp 기반 스트리밍 다운로더. 독립 실행형 GUI (별도 프로세스).

### 주요 클래스
- `DownloadItem` (dataclass) — 다운로드 항목
  - `status`: pending / downloading / merging / done / error / cancelled
  - `retry_count: int` — 순차 모드 자동 재시도 카운터
- `Downloader` — 단일 다운로드 실행기 (별도 스레드)
  - `cancel()` — `ydl.params['abort_download'] = True`
- `FormatPickerDialog` — URL 분석 후 포맷/파일명 선택
- `DownloaderApp(tk.Tk)` — 메인 앱

### 순차 다운로드 모드
- `_seq_var: tk.BooleanVar` — 🔁 순차 다운로드 체크박스
- `_seq_next()` — 트리 순서 기준 첫 번째 pending 항목 시작
- `_poll_ui()` 내 처리:
  - `done` 이벤트 → 300ms 후 `_seq_next()`
  - `error` 이벤트 + `retry_count < 3` → `retry_count++`, status='pending', 1.5초 후 재시도
  - `error` 이벤트 + `retry_count >= 3` → status='error'('실패'), 다음 항목으로

### EXE 빌드 시 유의사항
- `downloader.py` → `downloader.exe` 로 별도 빌드, `VidSort.exe` 옆 `downloader/` 폴더에 배치
- `vidsort.py._open_downloader()` 가 frozen 여부에 따라 `.exe` / `.py` 분기 실행

---

## 설정 파일 (vidsort_cfg.json)
```json
{
  "llm_token":          "GitHub PAT",
  "llm_model":          "claude-sonnet-4.5",
  "llm_endpoint":       "https://api.githubcopilot.com",
  "llm_prompt":         "...",
  "fc2ppvdb_email":     "user@example.com",
  "fc2ppvdb_password":  "...",
  "folders": [...],
  "formats": {...}
}
```

---

## 주요 패턴 & 주의사항

### LLM 호출
- **항상 `max_tokens=64000`** 사용 (GitHub Copilot 호출, 토큰 비용 무관)
- 스트리밍(SSE) 방식 — 대용량 응답도 타임아웃 없음
- JSON 파싱 실패 시 정규식 부분 복구 (`_llm_worker` 참고)
- Python 3 예외 변수 소멸: `lambda err=ex: f(err)` 패턴 사용

### 스레드 안전
- DB 쓰기 → `self.lock` 보유
- UI 업데이트 → 반드시 `widget.after(0, callback)`
- 워커 스레드에서 tk 위젯 직접 접근 금지

### 태그 rename/병합
```python
# UNIQUE 충돌 방지: UPDATE 대신 INSERT OR IGNORE + DELETE
INSERT OR IGNORE INTO tags(path, tag) SELECT path, ? FROM tags WHERE tag=?
DELETE FROM tags WHERE tag=?
```

### 정렬 방향
- `sort_asc_var = tk.BooleanVar(value=True)` — 툴바 ▲/▼ 버튼
- `query_page(..., sort_asc=None)`: None이면 종류별 기본값 (이름: ASC / 크기·날짜·재생시간: DESC)

### 웹 자동태그 2-phase
- Phase 1: 스크래핑 → `jav_raw` JSON 저장
- Phase 2: LLM 번역 → `jav_done=1`, alias, description, 태그 저장
- 장르: `_GENRE_MAP` 통과 필수 / 배우: `[:4]` 제한

### 사이드바 레이아웃
- `tk.PanedWindow(orient='horizontal')` — 드래그로 사이드바 너비 조절
- `_make_collapsible` toggle: `body.pack(fill='x', padx=0, after=hdr)` 필수 (`after=hdr` 없으면 맨 아래로 붙음)
- `_add_scroll(widget)` — `<MouseWheel>` / `<Button-4/5>` 이벤트를 `_tag_canvas`로 전달

### Canvas 썸네일 GC
- `_draw_card()` 에서 캐시 hit 시에도 반드시 `self._phs[path] = image` 저장
- `cv.itemconfigure(cw, width=e.width)` — `<Configure>` 이벤트의 `e.width` 사용 (`cv.winfo_width()` 불가)

### only_missing_thumb 1-shot 패턴
```python
# 사용 전:
self._only_missing_thumb = True
self._reload()
# _reload() 내부:
only_missing_thumb = self._only_missing_thumb
self._only_missing_thumb = False  # 즉시 초기화 → 1회만 적용
```

---

## 미완료 / 향후 작업

- **EXE 빌드**: `VidSort.spec` 미업데이트 — `httpx`, `flask`, `bs4`, `curl_cffi`, `python-vlc`, `downloader/` datas 추가 필요
- **AI 외부 검색 JavDB**: Cloudflare 차단으로 카드 0개 반환 빈번 — `curl_cffi` 없으면 동작 불안정
- **FC2DB 키워드 검색**: `/?s=` URL이 실제 검색 엔드포인트와 다를 수 있음 — HTML 덤프 로그로 확인 필요
- **갤러리 뷰 내 편집**: 웹 갤러리에서 태그/설명/제목 직접 수정 (미구현)
