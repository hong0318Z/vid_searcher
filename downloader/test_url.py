"""
URL 다운로드 가능 여부 진단 스크립트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

사용법:
  python test_url.py <URL>
  python test_url.py <URL> <Referer>
  python test_url.py <URL> "" --browser firefox
  python test_url.py <URL> "" --cookies cookies.txt

예시:
  python test_url.py https://kr2.ddalsney.com/video/category/general/450733
  python test_url.py https://kr2.ddalsney.com/video/... "" --browser firefox
  python test_url.py https://kr2.ddalsney.com/video/... "" --cookies cookies.txt
  python test_url.py https://n129.b-cdn.net/.VIDEO/KR_OS152/playlist.m3u8 https://원본사이트.com/

Cloudflare 차단 사이트 우회 방법 (권장 순서):
  1. --cookies cookies.txt   : 브라우저 확장(Get cookies.txt)으로 내보낸 파일 사용 — 가장 안정적
  2. --browser firefox       : Firefox 쿠키 자동 추출 (Firefox에서 사이트 접속 후)
  3. --browser edge          : Edge 쿠키 자동 추출
  4. curl_cffi<0.9.0 설치   : pip install "curl_cffi>=0.5.10,<0.9.0"
"""
import sys

# ── 의존성 확인 ────────────────────────────────
try:
    import yt_dlp
    print(f"[OK] yt-dlp {yt_dlp.version.__version__}")
except ImportError:
    print("[ERROR] yt-dlp 미설치  →  pip install yt-dlp")
    sys.exit(1)

try:
    import curl_cffi
    _ver = tuple(int(x) for x in curl_cffi.__version__.split('.')[:3])
    # yt-dlp 2026.x 기준: 0.5.10 <= ver < 0.9.0 만 지원 (0.9.x+ 는 API 변경으로 unsupported)
    if (0, 5, 10) <= _ver < (0, 9, 0):
        print(f"[OK] curl_cffi {curl_cffi.__version__}  (Cloudflare impersonation 가능)")
        HAS_CURL_CFFI = True
    else:
        print(f"[WARN] curl_cffi {curl_cffi.__version__} 은 yt-dlp 미지원 버전 → impersonation 불가")
        print(f"       해결: pip install \"curl_cffi>=0.5.10,<0.9.0\"")
        HAS_CURL_CFFI = False
except ImportError:
    print("[WARN] curl_cffi 미설치  →  pip install \"curl_cffi>=0.5.10,<0.9.0\"")
    HAS_CURL_CFFI = False

# ── 인자 파싱 ──────────────────────────────────
args = sys.argv[1:]

if not args:
    url = input("\nURL 입력: ").strip()
else:
    url = args[0]

referer = ""
browser = ""
cookie_file = ""

i = 1
while i < len(args):
    if args[i] == '--browser' and i + 1 < len(args):
        browser = args[i + 1]
        i += 2
    elif args[i] == '--cookies' and i + 1 < len(args):
        cookie_file = args[i + 1]
        i += 2
    elif not referer and not args[i].startswith('--'):
        referer = args[i]
        i += 1
    else:
        i += 1

print(f"\n{'='*60}")
print(f"URL     : {url}")
if referer:
    print(f"Referer : {referer}")
if cookie_file:
    print(f"쿠키파일: {cookie_file}")
elif browser:
    print(f"브라우저: {browser}")
print(f"{'='*60}\n")

# ── yt-dlp 옵션 ────────────────────────────────
opts = {
    'quiet':              False,
    'verbose':            True,
    'no_warnings':        False,
    'nocheckcertificate': True,
}

if HAS_CURL_CFFI:
    opts['extractor_args'] = {'generic': {'impersonate': ['']}}

if referer:
    opts['http_headers'] = {'Referer': referer}

if cookie_file:
    opts['cookiefile'] = cookie_file
    print(f"[INFO] 쿠키 파일 사용: {cookie_file}")
elif browser:
    opts['cookiesfrombrowser'] = (browser, None, None, None)
    print(f"[INFO] 브라우저 쿠키 사용: {browser}")

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
