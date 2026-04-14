"""
URL 다운로드 가능 여부 진단 스크립트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

사용법:
  python test_url.py <URL>
  python test_url.py <URL> <Referer>

예시:
  python test_url.py https://kr2.ddalsney.com/video/category/general/450733
  python test_url.py https://n129.b-cdn.net/.VIDEO/KR_OS152/playlist.m3u8 https://원본사이트.com/
"""
import sys
import json

# ── 의존성 확인 ────────────────────────────────
try:
    import yt_dlp
    print(f"[OK] yt-dlp {yt_dlp.version.__version__}")
except ImportError:
    print("[ERROR] yt-dlp 미설치  →  pip install yt-dlp")
    sys.exit(1)

try:
    import curl_cffi
    print(f"[OK] curl_cffi {curl_cffi.__version__}  (Cloudflare 우회 가능)")
    HAS_CURL_CFFI = True
except ImportError:
    print("[WARN] curl_cffi 미설치  →  pip install curl_cffi")
    HAS_CURL_CFFI = False

# ── 인자 파싱 ──────────────────────────────────
if len(sys.argv) >= 2:
    url = sys.argv[1]
else:
    url = input("\nURL 입력: ").strip()

referer = sys.argv[2] if len(sys.argv) >= 3 else ""

print(f"\n{'='*60}")
print(f"URL     : {url}")
if referer:
    print(f"Referer : {referer}")
print(f"{'='*60}\n")

# ── yt-dlp 옵션 ────────────────────────────────
opts = {
    'quiet':             False,
    'verbose':           True,    # 전체 디버그 출력
    'no_warnings':       False,
    'nocheckcertificate': True,
}

if HAS_CURL_CFFI:
    opts['extractor_args'] = {'generic': {'impersonate': ['']}}

if referer:
    opts['http_headers'] = {'Referer': referer}

# ── 분석 실행 ──────────────────────────────────
print("[분석 중...]\n")
try:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        print("\n[FAIL] 영상 정보를 가져올 수 없습니다.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("[SUCCESS] 분석 완료")
    print(f"{'='*60}")
    print(f"제목      : {info.get('title', '알 수 없음')}")
    print(f"재생시간  : {info.get('duration', '?')} 초")
    print(f"업로더    : {info.get('uploader') or info.get('channel') or '-'}")
    print(f"추출기    : {info.get('extractor', '-')}")

    fmts = info.get('formats', [])
    print(f"\n포맷 목록 ({len(fmts)}개):")
    print(f"  {'해상도':>10}  {'ext':>6}  {'크기':>10}  {'vcodec':>12}  비고")
    print(f"  {'-'*10}  {'-'*6}  {'-'*10}  {'-'*12}  ----")
    for f in fmts:
        h      = f.get('height') or '?'
        w      = f.get('width')  or ''
        res    = f'{w}x{h}' if w and h != '?' else (f'{h}p' if h != '?' else '?')
        ext    = f.get('ext', '?')
        size   = f.get('filesize') or f.get('filesize_approx')
        size_s = f'{size/1024/1024:.1f} MB' if size else '알 수 없음'
        vc     = (f.get('vcodec') or '-').split('.')[0][:12]
        note   = f.get('format_note') or ''
        print(f"  {res:>10}  {ext:>6}  {size_s:>10}  {vc:>12}  {note}")

except KeyboardInterrupt:
    print("\n[취소]")
    sys.exit(0)
except Exception as e:
    print(f"\n{'='*60}")
    print(f"[ERROR] {e}")
    print(f"{'='*60}")
    sys.exit(1)
