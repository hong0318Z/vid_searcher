"""
fc2ppvdb.com 스크래핑 가능성 테스트
★ curl_cffi 설치된 Windows 머신에서 실행하세요 ★
  pip install curl_cffi

실행: python _test_fc2ppvdb.py
"""

import json, re, sys, urllib.parse

EMAIL    = "hybrid0226@gmail.com"
PASSWORD = "hybrid0318!"
TEST_IDS = ["3181268", "1157452"]   # 테스트할 FC2-PPV ID

BASE = "https://fc2ppvdb.com/public"

# ── HTTP 엔진 선택 ──────────────────────────────
try:
    from curl_cffi.requests import Session as _CffiSession
    _ENGINE = 'curl_cffi'
    print('✅ curl_cffi 사용 (Cloudflare 우회)')
except ImportError:
    try:
        import httpx
        _ENGINE = 'httpx'
        print('⚠ httpx 사용 (Cloudflare 차단 가능)')
    except ImportError:
        import urllib.request, http.cookiejar, ssl as _ssl_mod
        _ENGINE = 'urllib'
        print('⚠ urllib 사용 (Cloudflare 차단 가능성 높음)')

print()

_HDRS_BASE = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'ja,ko;q=0.9,en-US;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

class Session:
    """엔진 통합 세션"""
    def __init__(self):
        if _ENGINE == 'curl_cffi':
            self._s = _CffiSession(impersonate='chrome110', verify=False)
        elif _ENGINE == 'httpx':
            self._s = httpx.Client(follow_redirects=True, verify=False, timeout=20)
        else:
            jar = http.cookiejar.CookieJar()
            ctx = _ssl_mod.create_default_context()
            ctx.check_hostname = False; ctx.verify_mode = _ssl_mod.CERT_NONE
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar),
                urllib.request.HTTPSHandler(context=ctx))

    def get(self, url, headers=None):
        h = {**_HDRS_BASE, 'Accept': 'text/html,*/*', **(headers or {})}
        if _ENGINE == 'curl_cffi':
            r = self._s.get(url, headers=h, timeout=20)
            return r.status_code, r.text
        elif _ENGINE == 'httpx':
            r = self._s.get(url, headers=h)
            return r.status_code, r.text
        else:
            req = urllib.request.Request(url, headers=h)
            try:
                with self._opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode('utf-8', errors='replace')
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode('utf-8', errors='replace')

    def post(self, url, data: dict, headers=None):
        h = {**_HDRS_BASE,
             'Content-Type': 'application/x-www-form-urlencoded',
             'Origin': 'https://fc2ppvdb.com',
             'Referer': f'{BASE}/login',
             **(headers or {})}
        encoded = urllib.parse.urlencode(data).encode()
        if _ENGINE == 'curl_cffi':
            r = self._s.post(url, data=data, headers=h, timeout=20)
            return r.status_code, r.text
        elif _ENGINE == 'httpx':
            r = self._s.post(url, data=data, headers=h)
            return r.status_code, r.text
        else:
            req = urllib.request.Request(url, data=encoded, headers=h)
            try:
                with self._opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode('utf-8', errors='replace')
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode('utf-8', errors='replace')

sess = Session()

# ═══════════════════════════════════════════════
# STEP 1: CSRF 토큰
# ═══════════════════════════════════════════════
print('='*55)
print('STEP 1: CSRF 토큰 획득')
print('='*55)

status, html = sess.get(f'{BASE}/login')
print(f'  status={status}  len={len(html)}')

csrf = ''
for pat in [r'<meta name="csrf-token" content="([^"]+)"',
            r'name="_token"[^>]+value="([^"]+)"',
            r'"csrfToken":"([^"]+)"']:
    m = re.search(pat, html)
    if m:
        csrf = m.group(1)
        break

if csrf:
    print(f'  ✅ CSRF: {csrf[:25]}...')
else:
    print('  ❌ CSRF 토큰 없음 — Cloudflare 차단 가능성')
    print(f'  HTML snippet: {html[:300]}')
    sys.exit(1)

print()

# ═══════════════════════════════════════════════
# STEP 2: 로그인
# ═══════════════════════════════════════════════
print('='*55)
print('STEP 2: 로그인')
print('='*55)

status, html = sess.post(f'{BASE}/login', {
    '_token': csrf, 'email': EMAIL, 'password': PASSWORD,
})
print(f'  status={status}  len={len(html)}')

logged_in = 'ログアウト' in html or 'logout' in html.lower()
if logged_in:
    print('  ✅ 로그인 성공')
else:
    # 오류 메시지 확인
    errs = re.findall(r'class="[^"]*(?:text-red|alert)[^"]*"[^>]*>([^<]{5,})<', html)
    print(f'  ❌ 로그인 실패  오류: {errs[:3]}')
    print(f'  HTML snippet: {html[:500]}')
    sys.exit(1)

print()

# ═══════════════════════════════════════════════
# STEP 3: 아티클 페이지 + API 패턴 탐색
# ═══════════════════════════════════════════════
for test_id in TEST_IDS:
    print('='*55)
    print(f'STEP 3: 아티클 {test_id} 탐색')
    print('='*55)

    # 3-A) 일반 HTML
    status, html = sess.get(f'{BASE}/articles/{test_id}')
    print(f'  [HTML] status={status}  len={len(html)}')
    m_info = re.search(r'id="article-info"[^>]*>([^<]*)<', html)
    if m_info:
        print(f'  article-info: {repr(m_info.group(1)[:80])}')

    # 3-B) JSON Accept (Laravel API 관행)
    status, body = sess.get(f'{BASE}/articles/{test_id}',
        headers={'Accept': 'application/json, text/plain, */*',
                 'X-Requested-With': 'XMLHttpRequest'})
    print(f'  [JSON Accept] status={status}  len={len(body)}')
    if body.strip().startswith('{'):
        try:
            d = json.loads(body)
            print(f'  JSON 키: {list(d.keys())[:12]}')
            for k in ('title','label','name','subject','タイトル','tags','actresses'):
                if k in d: print(f'    {k}: {str(d[k])[:80]}')
        except Exception as e:
            print(f'  JSON 파싱 오류: {e}')

    # 3-C) Inertia.js 패턴
    status, body = sess.get(f'{BASE}/articles/{test_id}',
        headers={'X-Inertia': 'true', 'Accept': 'application/json',
                 'X-Requested-With': 'XMLHttpRequest'})
    print(f'  [Inertia] status={status}  len={len(body)}')
    if body.strip().startswith('{'):
        try:
            d = json.loads(body)
            # Inertia는 {"component":"...", "props":{...}} 구조
            props = d.get('props', d)
            print(f'  Inertia props 키: {list(props.keys())[:12]}')
            art = props.get('article', props.get('data', {}))
            if art and isinstance(art, dict):
                for k in ('title','label','name','tags','actresses','writer'):
                    if k in art: print(f'    article.{k}: {str(art[k])[:80]}')
        except Exception as e:
            print(f'  Inertia 파싱 오류: {e}')

    # 3-D) 기타 API 경로
    for path in [
        f'/public/api/articles/{test_id}',
        f'/api/articles/{test_id}',
        f'/public/articles/{test_id}/data',
        f'/public/articles/show/{test_id}',
    ]:
        url = f'https://fc2ppvdb.com{path}'
        s, b = sess.get(url, headers={'Accept': 'application/json'})
        if s == 200 and len(b) > 200:
            is_j = b.strip().startswith('{') or b.strip().startswith('[')
            print(f'  ✅ [{s}] {path}  {"(JSON)" if is_j else "(HTML "+str(len(b))+"B)"}')
            if is_j:
                try:
                    d = json.loads(b)
                    print(f'    키: {list(d.keys())[:10]}')
                except Exception:
                    pass
        elif s not in (404, 403, 405):
            print(f'  [{s}] {path}')
    print()

# ═══════════════════════════════════════════════
# STEP 4: JS 번들에서 API URL 패턴 추출
# ═══════════════════════════════════════════════
print('='*55)
print('STEP 4: JS 번들에서 API 경로 탐색')
print('='*55)

_, page_html = sess.get(f'{BASE}/articles/{TEST_IDS[0]}')
js_urls = re.findall(r'src="(https://fc2ppvdb\.com/[^"]+\.js[^"]*)"', page_html)
print(f'  JS 파일 {len(js_urls)}개')

for js_url in js_urls[:2]:
    print(f'  분석: {js_url[-60:]}')
    s, js_body = sess.get(js_url)
    if s == 200:
        # /articles/, /api/ 관련 경로 추출
        found = set(re.findall(r'["\`](/(?:public/)?(?:api/)?articles[^"\'` ]{1,60})["\`]', js_body))
        found |= set(re.findall(r'["\`](/[^"\'` ]*article[^"\'` ]{1,60})["\`]', js_body))
        for p in sorted(found)[:20]:
            print(f'    {p}')
    print()

print('='*55)
print('테스트 완료 — 위 결과로 API 엔드포인트 판단')
print('='*55)
