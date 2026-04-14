# 스트리밍 다운로더 — 시스템 가이드

> LLM이 새로운 기능을 추가하거나 수정할 때 이 파일을 먼저 읽어 전체 구조를 파악하세요.

---

## 개요

`downloader.py` — Python + tkinter 기반 GUI 다운로더.  
yt-dlp 를 백엔드로 사용해 YouTube, Twitch, FC2, 니코니코 등 1000+ 사이트의 스트리밍 영상을 다운로드.  
URL 입력 → 포맷 자동 추출 → 선택 → 다운로드 전체 흐름을 단일 파일로 구현.

---

## 파일 구조

```
downloader/
├── downloader.py        # 메인 앱 (단일 파일, 전체 로직)
├── requirements.txt     # 의존성 목록
├── DOWNLOADER.md        # 이 파일 — 시스템 가이드
└── downloader_cfg.json  # 설정 저장 (런타임 생성, git 제외)
```

---

## 아키텍처 — 클래스/함수 구조

### 데이터 모델

```python
@dataclass
class DownloadItem:
    uid:          str    # 8자리 uuid (Treeview iid 로도 사용)
    url:          str    # 원본 URL
    save_dir:     str    # 저장 경로
    quality:      str    # 표시용 레이블 ('자동 최고화질' / '1920x1080' 등)
    format_str:   str    # yt-dlp format string ('bestvideo+bestaudio/best' 등)
    custom_title: str    # 사용자 지정 파일명 (빈 문자열이면 사이트 제목 사용)
    status:       str    # pending / downloading / merging / done / error / cancelled
    title:        str    # yt-dlp 가 반환한 영상 제목
    filename:     str    # 실제 출력 파일 경로
    progress:     float  # 0~100
    speed:        str    # '2.3 MB/s' 형식
    eta:          str    # '1m 23s' 형식
    size:         str    # '1.2 GB' 형식
    error:        str    # 오류 메시지
```

---

### `class Downloader`

단일 다운로드 실행기. **별도 스레드**에서 `run()` 호출.

| 메서드 | 역할 |
|---|---|
| `run()` | yt-dlp 호출, 진행상황 콜백 발화 |
| `cancel()` | `yt_dlp.params['abort_download'] = True` 로 중단 |
| `_hook(d)` | 다운로드 진행 hook — `on_progress` 콜백 호출 |
| `_postproc_hook(d)` | 병합 시작 시 status → `'merging'` |

**포맷 결정 우선순위** (`run()` 내부):
```
item.format_str  →  없으면  'bestvideo+bestaudio/best'
```

**파일명 결정** (`run()` 내부):
```
item.custom_title 있음  →  <custom_title>.%(ext)s
없음                    →  %(title)s.%(ext)s  (사이트 제목)
```

---

### `class FormatPickerDialog(tk.Toplevel)`

URL 분석 결과를 보여주는 모달 다이얼로그. `DownloaderApp._on_analyze_done()` 에서 생성.

**UI 구성:**
```
┌──────────────────────────────────────────────────┐
│  파일명 (수정 가능)  [Entry — 사이트 제목 자동 입력]  │
│  재생시간: X:XX   채널/업로더: ...                  │
│  ─────────────────────────────────────────────   │
│  포맷 선택 (더블클릭 또는 Enter 로 추가)             │
│  ┌────────┬────┬──────┬────┬────────┬─────────┐  │
│  │해상도  │확장│크기  │코덱│재생시간│비고     │  │
│  │자동최고│mp4 │  -   │ - │X:XX    │자동병합 │  │
│  │1920x..│mp4 │1.2GB │avc│X:XX    │         │  │
│  │ ...   │    │      │   │        │         │  │
│  └────────┴────┴──────┴────┴────────┴─────────┘  │
│                         [취소]  [대기열에 추가]    │
└──────────────────────────────────────────────────┘
```

**포맷 분류 로직** (`_fill_formats()`):

| 줄 | 조건 | yt-dlp format string |
|---|---|---|
| 자동 최고화질 | 항상 첫 줄 | `bestvideo+bestaudio/best` |
| 복합 포맷 | `vcodec != 'none'` AND `acodec != 'none'` | `format_id` 직접 지정 |
| DASH 분리 스트림 | `vcodec != 'none'` AND `acodec == 'none'` | `bestvideo[height<=H]+bestaudio/best[height<=H]/best` |
| 오디오만 | `vcodec == 'none'` AND `acodec != 'none'` | `bestaudio/best` |

높이 중복 제거: `seen_h` set 으로 같은 높이의 포맷은 1개만 표시.

---

### `class DownloaderApp(tk.Tk)` — 메인 윈도우

#### 상태 변수

| 변수 | 타입 | 역할 |
|---|---|---|
| `_items` | `dict[str, DownloadItem]` | uid → DownloadItem 전체 목록 |
| `_workers` | `dict[str, Downloader]` | uid → 실행 중인 Downloader |
| `_ui_queue` | `queue.Queue` | 워커 스레드 → 메인 스레드 이벤트 전달 |
| `_analyzing` | `bool` | 분석 중 중복 요청 방지 플래그 |

#### URL 분석 흐름 (비동기)

```
_analyze_url()
  │  백그라운드 스레드 시작
  │  버튼 비활성화, "분석 중..." 표시
  ▼
_worker() [별도 스레드]
  │  yt_dlp.extract_info(url, download=False)
  │  after(0, ...)  ← 메인 스레드로 결과 전달
  ▼
_on_analyze_done()  또는  _on_analyze_error()
  │  버튼 재활성화
  │  FormatPickerDialog 생성
  ▼
_enqueue()  ← FormatPickerDialog.on_confirm 콜백
  │  DownloadItem 생성 → _items 등록 → Treeview 삽입
```

#### 다운로드 실행 흐름

```
_start_selected()
  ▼
_run_download(item)
  │  Downloader 생성, 별도 스레드에서 run()
  │  on_progress / on_done / on_error → _ui_queue.put(...)
  ▼
_poll_ui()  [after(200, ...) 반복]
  │  _ui_queue 에서 이벤트 소비
  ▼
_update_row(item)  ← Treeview 행 갱신
```

#### 주요 메서드 목록

| 메서드 | 역할 |
|---|---|
| `_style()` | TTK 다크 테마 설정. `Picker.Treeview` 별도 스타일(rowheight=28) 포함 |
| `_build_ui()` | 전체 UI 구성 (URL 입력 / 저장 경로 / 분석 버튼 / Treeview / 버튼바 / 로그) |
| `_analyze_url()` | 입력 검증 후 백그라운드 분석 스레드 시작 |
| `_on_analyze_done()` | 분석 완료 시 FormatPickerDialog 열기 |
| `_on_analyze_error()` | 분석 실패 시 로그에 오류 출력 |
| `_enqueue()` | FormatPickerDialog 콜백 — DownloadItem 생성 및 Treeview 추가 |
| `_start_selected()` | 선택된(없으면 전체) pending 항목 다운로드 시작 |
| `_run_download(item)` | Downloader 스레드 생성 |
| `_cancel_selected()` | 선택 항목 취소 |
| `_remove_done()` | 완료/취소/오류 항목 일괄 제거 |
| `_remove_selected()` | 선택 항목 제거 (진행 중이면 취소 후 제거) |
| `_poll_ui()` | 200ms 간격으로 _ui_queue 소비, Treeview 갱신 |
| `_update_row(item)` | DownloadItem 상태를 Treeview 행에 반영 |
| `_log_msg(msg, color)` | 하단 로그 텍스트에 타임스탬프 메시지 추가 |

---

## 유틸 함수

| 함수 | 역할 |
|---|---|
| `_fmt_speed(bps)` | `2.3 MB/s` 형식 변환 |
| `_fmt_eta(sec)` | `1m 23s` 형식 변환 |
| `_fmt_size(bytes)` | `1.2 GB` 형식 변환 |
| `_fmt_duration(sec)` | `1:23:45` 또는 `5:30` 형식 변환 |
| `_sanitize_filename(name)` | `\/:*?"<>|` 제거, 최대 200자 |
| `_shorten_url(url, maxlen)` | URL 이 너무 길면 `...` 처리 |
| `load_cfg()` / `save_cfg(cfg)` | `downloader_cfg.json` 읽기/쓰기 |

---

## 설정 파일 (downloader_cfg.json)

런타임에 자동 생성됨. 수동 편집 가능.

```json
{
  "save_dir": "C:/Users/user/Downloads"
}
```

---

## 의존성

| 패키지 | 필수 여부 | 역할 |
|---|---|---|
| `yt-dlp` | **필수** | 영상 추출 및 다운로드 엔진 |
| `ffmpeg` (시스템) | 권장 | 비디오+오디오 병합. 없으면 단일 스트림만 가능 |
| `curl_cffi` | 선택 | Cloudflare 우회 (FC2 등) |

---

## 스레드 안전 규칙

- **워커 스레드** (`Downloader.run`, `_analyze_url._worker`) 에서 tkinter 위젯 직접 접근 **금지**
- UI 업데이트는 반드시 `self.after(0, callback)` 또는 `_ui_queue.put(...)` 경유
- `_ui_queue` → `_poll_ui()` (200ms 폴링) → `_update_row()` 순서로 Treeview 갱신

---

## 미완료 / 향후 작업

- **다중 URL 일괄 입력**: 텍스트박스에 여러 URL 붙여넣기 후 일괄 분석
- **다운로드 자동 시작**: 대기열 추가 즉시 다운로드 시작 옵션 (설정 토글)
- **썸네일 미리보기**: 포맷 선택 다이얼로그에 썸네일 표시
- **재시도 버튼**: 오류 항목 개별 재시도
- **쿠키 지원**: 로그인 필요 사이트를 위한 브라우저 쿠키 연동 (`--cookies-from-browser`)
- **프록시 설정**: 설정 다이얼로그에 HTTP/SOCKS 프록시 입력란 추가
- **VidSort 연동**: 다운로드 완료 후 자동으로 VidSort DB에 등록
