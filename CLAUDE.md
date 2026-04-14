# VidSort v5 — Claude Code 시스템 가이드

> 새 세션 시작 시 이 파일을 먼저 읽어서 전체 구조를 파악하세요.

---

## 프로젝트 개요

로컬 영상 파일 관리 데스크탑 앱 (Python + tkinter).  
태그·별칭·설명 관리, 웹 자동태그(FC2 스크래핑+LLM), AI 추천 검색, VLC 인라인 플레이어, 웹 갤러리 뷰 포함.

**개발 브랜치**: `claude/improve-ui-video-recommendation-3QR5p`  
**배포 형태**: 미정 (EXE 단일 파일 예정, 아직 빌드 설정 미업데이트)

---

## 파일 구조

```
vid_searcher/
├── vidsort.py           # 메인 앱 (~5000줄) — DB, UI, 모든 비즈니스 로직
├── jav_scraper.py       # FC2/JAV 메타 스크래퍼
├── llm_api.py           # GitHub Copilot LLM 클라이언트
├── web_gallery.py       # Flask 웹 갤러리 서버
├── _test_fc2ppvdb.py    # fc2ppvdb.com API 테스트 스크립트 (개발용)
├── VidSort.spec         # PyInstaller 빌드 설정 (미업데이트)
├── vidsort.db           # SQLite DB (런타임 생성)
├── vidsort_cfg.json     # 설정 저장 (런타임 생성)
└── .thumbs/             # 썸네일 캐시 (md5해시.jpg)
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
HAS_VLC    = True/False  # python-vlc 설치 여부 (Windows: PYTHON_VLC_LIB_PATH 자동 설정)
```

### VLC 초기화 (`_setup_vlc_path()`)
모듈 상단에서 Windows 레지스트리/Program Files에서 libvlc.dll 경로를 찾아
`os.environ['PYTHON_VLC_LIB_PATH']`에 설정. `HAS_VLC` 플래그로 가용 여부 관리.

### class DB (line ~126)
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
- `query_page(..., sort_asc=None)` — 검색/필터/페이징. `sort_asc` 없으면 정렬 종류별 기본값 사용
- `rename_tag(old, new)` — INSERT OR IGNORE + DELETE 방식 (병합 안전)
- `delete_tag(tag)` — tags + tag_meta 전체 삭제
- `get_jav_done_list(search, limit)` — `jav_raw!=''` OR `jav_done=1 AND alias LIKE '%[%-%]%'`
- `reset_jav(path)` — jav_done=0, jav_raw='', alias='', description='' + 태그 삭제
- `get_fc2_fallback_list(limit)` — source='fc2-fallback'인 항목 반환 (재스크래핑 대상)
- `update_jav_raw(path, raw_json)` — jav_raw만 업데이트 (alias/태그 유지)
- `recommend_search(tags, keywords, limit)` — AI 추천용 검색 (150개 랜덤 후 50개 반환)

### class CanvasGrid (line ~616)
Canvas 기반 커스텀 영상 썸네일 그리드. 500+ 영상 배치 렌더링.

- `on_open` → 더블클릭 시 `_viewer_dlg` 연결
- `on_ctx` → 우클릭 메뉴
- `load()` / `hard_load()` — 소프트/하드 리렌더링

### class VidSort(tk.Tk)
메인 윈도우. 주요 메서드 그룹:

**UI 빌드**
- `_build_ui()` — 상단 검색바 + 툴바 + 메인 영역 + 사이드바
- `_build_sidebar()` — 접이식 섹션 구조 (폴더/태그/AI). 사이드바 너비 265px
  - `_make_collapsible(parent, title, ...)` — 토글 섹션 헬퍼
  - `_add_scroll()` / `_fwd_scroll()` — 태그 버튼 휠 이벤트를 `_tag_canvas`로 전달
- `_style()` — TTK 다크 테마

**검색/정렬**
- `_reload()` — 검색 조건 수집 → 스레드로 `_bg_query` 실행
- `sort_asc_var` — `tk.BooleanVar` (기본 True). 툴바 ▲/▼ 버튼으로 토글
- `_toggle_sort_dir()` — 정렬 방향 전환 + `_reload()` 호출
- `folder_search_var` — 툴바 '폴더명' 체크박스 변수

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

**태그 관리** (`_tag_manage_dlg`)
- 왼쪽: 태그 목록 리스트박스
- 오른쪽:
  - **이름 변경 Entry + `✏ 이름 변경` 버튼** (Enter 키 지원)
    - 기존에 없는 이름 → 단순 rename
    - 기존에 있는 이름 → 파일 수 표시 후 병합 확인 → `rename_tag()` 호출
  - 설명 편집 + `💾 설명 저장`
  - `🗑 태그 삭제`
- LLM 태그 번역 (일본어→한국어)
- LLM 태그 통합 (유사 태그 그룹화 → 대표 태그로 병합)

**AI 추천 검색** (`_ai_recommend_dlg`)
- 자연어 입력 → 1차 LLM 호출 (`recommend_query`) → 태그+키워드 추출
- DB 검색 (`recommend_search`) → 150개 후보 랜덤 셔플 → 50개
- 2차 LLM 호출 (`recommend_explain`) → 스트리밍으로 가게 점원 스타일 설명
- 결과 썸네일 그리드 + LLM 설명 패널

**웹 자동태그 기능** (구 "JAV 처리")
- `_jav_process_dlg()` — 웹 자동태그 메인 다이얼로그 (2탭: 스크래핑 / LLM 번역)
  - Tab1: 미처리 파일 목록, 우클릭 제외
  - Tab2: 스크래핑 완료(LLM 대기) 목록, LLM 일괄 번역
  - LLM 응답 디버그 패널 (토글)
- `_jav_db_dlg()` — 웹태그 DB 뷰어
  - `🔄 FC2 재스크래핑` 버튼 — fc2-fallback 항목 재스크래핑 (백그라운드)
  - 태그 번역, 초기화 버튼
- `_jav_exclude(paths)` — 대상에서 제외 (jav_done=1)
- `_reset_tags_dlg(paths)` — 태그/메타 전체 초기화 → 목록에 재등장
- `_llm_worker()` (내부) — 배치 LLM 호출 → `_GENRE_MAP` 통과 → DB 저장

**`_GENRE_MAP`** (line ~3850)
영어/일본어 장르 → 한국어 변환 딕셔너리. JAV LLM 워커에서 필수 통과.

**설정 다이얼로그** (`_llm_settings_dlg`)
- GitHub Copilot 토큰/모델/엔드포인트
- **FC2PPVDB 이메일/비밀번호** (fc2ppvdb.com 스크래핑용)
- 태그 분류 시스템 프롬프트 편집
- `_apply_fc2ppvdb_creds()` — 저장 시 `jav_scraper.set_fc2ppvdb_credentials()` 호출

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
- `_chat_tracked(messages, max_tokens, on_chunk)` → (content, tok_in, tok_out)
  - 스트리밍(SSE) 방식, read timeout=60s
- `analyze_and_tag(filenames, tag_pool, on_progress, custom_prompt)` — 배치 자동 태그
- `analyze_and_name(filenames, on_progress)` — 한글 이름+설명 생성
- `recommend_query(user_query, tag_pool, on_chunk)` → `{"tags":[...], "keywords":[...], "intent":"..."}`
  - 자연어 쿼리 → DB 검색용 태그+키워드 추출
- `recommend_explain(user_query, videos, on_chunk)` → str
  - 찾은 영상 목록 → 가게 점원 스타일 스트리밍 설명

---

## jav_scraper.py 구조

**스크래핑 순서** (`fetch_meta_verbose`):
```
일반 AV 코드:
  1. 오프라인 JSON (jav_offline.json)
  2. R18.dev 공식 API
  3. JavDB 스크래핑
  4. Javbus 스크래핑

FC2-PPV 코드:
  0-a. fc2db.net 스크래핑 (로그인 불필요)
  0-b. fc2ppvdb.com API (로그인 필요, 계정 설정 시)
  0-c. fallback 최소 메타 (FC2/FC2-PPV/아마추어 태그)
```

**`extract_code(filename)`** — 파일명에서 AV/FC2 코드 추출

**HTTP 엔진** (우선순위):
1. `curl_cffi` (impersonate='chrome110') — Cloudflare 우회
2. `httpx` — 일반
3. `urllib` — 내장 폴백

**FC2 전용 스크래퍼**:
- `_fetch_fc2db(code)` — `https://fc2db.net/work/{number}/`
  - JSON-LD 우선 파싱, HTML 보완 (`h2.font-extrabold a`)
  - HTML < 5KB → 소프트 404 감지 (미등록 ID)
  - tags: `a[href*="work-tags"]`, seller: `a[href*="/seller/"]`
  - date/duration: `dt` + `find_next_sibling('dd')`

- `_fetch_fc2ppvdb(code)` — `https://fc2ppvdb.com/public/articles/article-info?videoid={id}`
  - `set_fc2ppvdb_credentials(email, password)` 사전 호출 필요
  - `_ppvdb_login()` — 세션 쿠키 획득 (curl_cffi Session / httpx Client)
  - 모듈 레벨 `_ppvdb_sess` 세션 재사용 (로그인 1회)
  - `isLoggedIn == 0` 감지 시 자동 재로그인 (1회 재귀)
  - 반환 필드: title, actresses[], tags[], writer, release_date, duration, image_url

**FC2PPVDB JSON 응답 구조**:
```json
{
  "isLoggedIn": 2,
  "article": {
    "title": "...", "video_id": 3181268,
    "release_date": "2023-02-08", "duration": "01:04:34",
    "image_url": "https://...",
    "writer": {"name": "..."},
    "actresses": [{"name": "..."}],
    "tags": [{"name": "素人"}, ...]
  }
}
```

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
- 스트리밍 방식 — 대용량 응답도 타임아웃 없음
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
태그 관리 UI에서 기존 이름으로 변경 시 파일 수 표시 후 자동 병합.

### 정렬 방향
- `sort_asc_var = tk.BooleanVar(value=True)` — 툴바 ▲/▼ 버튼
- `query_page(..., sort_asc=None)`: None이면 정렬 종류별 기본값
  - 이름: ASC 기본 / 크기·날짜·재생시간: DESC 기본

### 웹 자동태그 2-phase
- Phase 1: 스크래핑 → `jav_raw` JSON 저장 (`set_jav_raw`)
- Phase 2: LLM 번역 → `jav_done=1`, alias, description, 태그 저장
- 장르: `genres_ko` + `meta.get('genres')` → `_GENRE_MAP` → DB (개수 제한 없음)
- 배우: `[:4]` 제한 유지

### 더블클릭 동작
- 더블클릭 → `_viewer_dlg()` (VLC 있으면 인라인 재생, 없으면 썸네일)
- `▶ 외부 앱` 버튼 or VLC 없을 때 썸네일 클릭 → 외부 플레이어

### 사이드바 태그 휠 스크롤
- `_add_scroll(widget)` — `<MouseWheel>`, `<Button-4/5>` 이벤트를 `self._tag_canvas`로 전달
- 각 태그 버튼 생성 시 `_add_scroll()` 호출

### Canvas 스크롤 UI
- `cv.itemconfigure(cw, width=e.width)` — `<Configure>` 이벤트의 `e.width` 사용
  (`cv.winfo_width()` 불가 — 실현 전 0 반환)
- `selectcolor='#7c6ff7'` 사용 (배경색과 다른 색이어야 체크박스가 보임)

---

## 미완료 / 향후 작업

- **EXE 빌드**: `VidSort.spec` 미업데이트 — `httpx`, `flask`, `bs4`, `curl_cffi`, `python-vlc` hidden imports 및 `web_gallery.py` datas 추가 필요
- **갤러리 뷰 내 편집**: 웹 갤러리에서 태그/설명/제목 직접 수정 (미구현)
- **FC2PPVDB 세션 만료**: 장시간 실행 시 쿠키 만료 → 자동 재로그인 구현됨 (1회 재귀)
