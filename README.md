# VidSort v3 — 설치 가이드

## 설치

```bash
pip install pillow pyinstaller
winget install ffmpeg        # 또는 ffmpeg.exe를 vidsort.py 옆에 복사
```

## 실행

```bash
python vidsort.py
```

## exe 빌드

```bash
pyinstaller --onefile --windowed --name VidSort vidsort.py
```

→ `dist/VidSort.exe`  
⚠️ exe 옆에 `ffmpeg.exe` + `ffprobe.exe` 복사 필요

---

## DB / 썸네일 저장 위치

| 파일 | 위치 |
|------|------|
| `vidsort.db` | `vidsort.py` 와 같은 폴더 |
| `.thumbs/` | `vidsort.py` 와 같은 폴더 |

→ 프로그램 폴더를 통째로 옮겨도 DB가 따라옵니다

---

## 주요 기능

- **📁 폴더 추가** — 여러 폴더를 등록해서 통합 관리
- **🔍 통합 검색** — 파일명 + 별칭 + 태그 동시 검색
- **✏ 별칭** — 파일명과 별개로 커스텀 이름 지정 (우클릭 → 별칭 편집)
- **🏷 태그** — 직접 입력한 태그, 여러 개 가능 (우클릭 → 태그 편집)
- **썸네일** — 영상 비율 그대로 표시, 백그라운드 병렬 생성
- **48개씩 배치 렌더** — 수천 개도 UI 블로킹 없음
- **스캔 최적화** — 파일 목록만 빠르게 수집 후 썸네일은 별도 처리
- **NAS 자동 감지** — 네트워크 드라이브면 워커 수 자동 조절
- **✂ 잘라내기/복사** — 썸네일 보면서 바로 파일 이동
- **더블클릭 재생** — Windows 기본 연결 앱으로 재생

## v2 대비 변경

- 스캔 시 ffprobe 실행 안 함 → 훨씬 빠른 스캔
- 썸네일 비율 유지 (억지로 채우지 않음)
- DB 위치 고정 (프로그램 폴더)
- 배치 렌더링으로 UI 응답성 개선
