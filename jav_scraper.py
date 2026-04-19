"""
JavDB / Javbus 스크래퍼
AV 코드 → 제목·배우·장르·설명 조회

조회 순서:
  1. 로컬 오프라인 JSON 덤프  (jav_offline.json)
  2. JavDB 라이브 스크래핑
  3. Javbus 라이브 스크래핑 (fallback)

오프라인 JSON 형식 (코드는 대문자, 하이픈 포함):
{
  "PRED-123": {
    "title": "원제",
    "actresses": ["배우1", "배우2"],
    "genres":    ["장르1", "장르2"],
    "studio":    "스튜디오명",
    "date":      "2024-01-01"
  },
  ...
}
"""

import re, time, random, json, urllib.parse
from pathlib import Path

try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

# 브라우저에 최대한 가깝게 — Cloudflare 봇 차단 우회
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection':      'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest':  'document',
    'Sec-Fetch-Mode':  'navigate',
    'Sec-Fetch-Site':  'none',
    'Sec-Fetch-User':  '?1',
    'Cache-Control':   'max-age=0',
}

# ─────────────────────────────────────────────────
#  코드 추출
# ─────────────────────────────────────────────────
_CODE_RE = re.compile(
    r'(?<![A-Z0-9])([A-Z]{2,8})-?(\d{2,6})(?![A-Z0-9])', re.IGNORECASE)

def extract_code(filename: str) -> str | None:
    """파일명에서 AV 코드 추출.
    예: SSIS-001, IPX123 → SSIS-001 / IPX-123
        FC2-PPV-1234567, FC2PPV1234567 → FC2-PPV-1234567"""
    stem = Path(filename).stem.upper()

    # FC2-PPV 우선 처리 (숫자 6-8자리)
    fc2 = re.search(r'FC2[-_]?PPV[-_]?(\d{4,8})', stem)
    if fc2:
        return f'FC2-PPV-{fc2.group(1)}'

    # 괄호 안 코드 우선
    m = re.search(r'[\[\(]([A-Z]{2,8}-?\d{2,6})[\]\)]', stem)
    if m:
        raw = m.group(1)
    else:
        m2 = _CODE_RE.search(stem)
        if not m2:
            return None
        raw = m2.group(0)
    # 하이픈 정규화
    parts = re.match(r'([A-Z]+)-?(\d+)', raw.upper())
    if not parts:
        return None
    letters, digits = parts.group(1), parts.group(2)
    # 숫자는 3자리 이상 zero-pad (관습)
    digits = digits.zfill(3)
    return f'{letters}-{digits}'

# ─────────────────────────────────────────────────
#  HTTP  (재시도 + verify=False로 TLS 지문 문제 우회)
# ─────────────────────────────────────────────────
def _get(url: str, timeout: int = 20, cookies: dict | None = None,
         referer: str = ''):
    headers = dict(_HEADERS)
    if referer:
        headers['Referer'] = referer
    # curl_cffi cookies 파라미터가 무시되는 경우 대비 — Cookie 헤더에도 직접 삽입
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())

    last_exc = None
    for attempt in range(3):
        if attempt:
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f'[_get] 재시도 {attempt}/2 ({wait:.1f}s 후) {url}', flush=True)
            time.sleep(wait)
        try:
            # curl_cffi: Chrome TLS 지문 완벽 복제 → Cloudflare 우회 최강
            if _HAS_CFFI:
                r = _cffi_requests.get(
                    url, headers=headers,
                    cookies=cookies or {},
                    timeout=timeout,
                    impersonate='chrome110',
                    allow_redirects=True,
                    verify=False,
                )
                r.status = r.status_code
                return r
            elif _HAS_HTTPX:
                with httpx.Client(
                        timeout=timeout,
                        follow_redirects=True,
                        headers=headers,
                        cookies=cookies or {},
                        verify=False,
                ) as c:
                    r = c.get(url)
                    r.status = r.status_code
                    return r
            else:
                import urllib.request, ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    class _R:
                        status = resp.status
                        text   = resp.read().decode('utf-8', errors='replace')
                    return _R()
        except Exception as e:
            last_exc = e
            print(f'[_get] 시도{attempt+1} 실패: {type(e).__name__}: {e}', flush=True)

    # 모든 재시도 실패
    class _Err:
        status = 0
        text   = ''
        _err   = last_exc
    print(f'[_get] 최종 실패: {last_exc}', flush=True)
    return _Err()

def _soup(html: str):
    return BeautifulSoup(html, 'html.parser')

# ─────────────────────────────────────────────────
#  JavDB
# ─────────────────────────────────────────────────
JAVDB_BASE = 'https://javdb.com'

def _fetch_javdb(code: str) -> tuple:
    """(meta_dict | None, error_str) 반환"""
    if not _HAS_BS4:
        return None, 'beautifulsoup4 미설치'
    try:
        url_search = f'{JAVDB_BASE}/search?q={code}&f=all'
        print(f'[JavDB] GET {url_search}', flush=True)
        r = _get(url_search)
        print(f'[JavDB] status={r.status}', flush=True)
        if r.status != 200:
            return None, f'JavDB 검색 HTTP {r.status}'
        soup = _soup(r.text)

        # 코드 일치 카드 우선, 없으면 첫 번째
        movie_path = None
        cards = soup.select('.movie-list .item, .search-video-section .item')
        print(f'[JavDB] 검색결과 카드 {len(cards)}개', flush=True)
        for card in cards:
            uid = card.select_one('.uid, .video-title strong')
            if uid and uid.get_text(strip=True).upper() == code.upper():
                a = card.select_one('a')
                if a:
                    movie_path = a.get('href', '')
                    break
        if not movie_path:
            a = soup.select_one('.movie-list .item a, .search-video-section .item a')
            if a:
                movie_path = a.get('href', '')
        if not movie_path:
            return None, f'JavDB 검색결과에 {code} 없음 (카드:{len(cards)}개)'

        time.sleep(0.8)
        url = JAVDB_BASE + movie_path if movie_path.startswith('/') else movie_path
        print(f'[JavDB] 상세 GET {url}', flush=True)
        r2  = _get(url)
        print(f'[JavDB] 상세 status={r2.status}', flush=True)
        if r2.status != 200:
            return None, f'JavDB 상세페이지 HTTP {r2.status}'
        s = _soup(r2.text)

        result = {'code': code, 'source': 'javdb', 'url': url}

        # 제목
        for sel in ('h2.title .current-title', 'strong.current-title',
                    'h2.title', '.video-detail h2'):
            el = s.select_one(sel)
            if el:
                result['title'] = el.get_text(strip=True)
                break
        result.setdefault('title', '')

        # 패널 파싱 (배우, 장르, 날짜, 스튜디오)
        actresses, genres = [], []
        for row in s.select('.panel-block'):
            lbl = (row.select_one('strong') or row.select_one('span')).get_text(strip=True) \
                  if row.find(['strong','span']) else ''
            vals = [a.get_text(strip=True) for a in row.select('a')]
            if not vals:
                vals = [row.select_one('span.value').get_text(strip=True)] \
                       if row.select_one('span.value') else []
            lbl_up = lbl.upper()
            if any(k in lbl_up for k in ('女優','ACTOR','배우','演員')):
                actresses = vals
            elif any(k in lbl_up for k in ('ジャンル','類別','TAG','GENRE','장르')):
                genres = vals
            elif any(k in lbl_up for k in ('日期','DATE','발매','発売')):
                result['date'] = vals[0] if vals else ''
            elif any(k in lbl_up for k in ('メーカー','片商','STUDIO','스튜디오','廠商')):
                result['studio'] = vals[0] if vals else ''

        result['actresses'] = actresses
        result['genres']    = genres
        result.setdefault('date', '')
        result.setdefault('studio', '')

        # 커버
        for sel in ('.video-cover img', 'img.video-cover', '.column.column-video-cover img'):
            img = s.select_one(sel)
            if img:
                result['cover_url'] = img.get('src') or img.get('data-src', '')
                break
        result.setdefault('cover_url', '')

        print(f'[JavDB] 제목="{result["title"]}" 배우={actresses} 장르={genres}', flush=True)
        if result['title']:
            return result, ''
        return None, 'JavDB 제목 파싱 실패 (HTML 구조 변경 가능성)'

    except Exception as e:
        import traceback
        msg = f'JavDB 예외: {e}'
        print(f'[jav_scraper] {msg}\n{traceback.format_exc()}', flush=True)
        return None, msg

# ─────────────────────────────────────────────────
#  Javbus (fallback)
# ─────────────────────────────────────────────────
JAVBUS_BASE    = 'https://www.javbus.com'
# 성인인증 쿠키 없으면 연령확인 페이지 HTML이 반환됨 → title/배우 파싱 실패
_JAVBUS_COOKIES = {'over18': '1', 'age': '1'}

def _fetch_javbus(code: str) -> tuple:
    """(meta_dict | None, error_str) 반환"""
    if not _HAS_BS4:
        return None, 'beautifulsoup4 미설치'
    try:
        url_direct = f'{JAVBUS_BASE}/{code}'
        print(f'[Javbus] GET {url_direct}', flush=True)
        r = _get(url_direct, referer=JAVBUS_BASE, cookies=_JAVBUS_COOKIES)
        print(f'[Javbus] status={r.status}', flush=True)
        if r.status != 200:
            # 검색 시도
            url_search = f'{JAVBUS_BASE}/search/{code}'
            print(f'[Javbus] 검색 GET {url_search}', flush=True)
            r = _get(url_search, referer=JAVBUS_BASE, cookies=_JAVBUS_COOKIES)
            print(f'[Javbus] 검색 status={r.status}', flush=True)
            if r.status != 200:
                return None, f'Javbus HTTP {r.status} (Cloudflare 차단 가능성)'
            s    = _soup(r.text)
            a    = s.select_one('.movie-box')
            if not a:
                return None, 'Javbus 검색결과 파싱 실패'
            href = a.get('href', '')
            time.sleep(0.8)
            r    = _get(href, referer=url_search, cookies=_JAVBUS_COOKIES)
            if r.status != 200:
                return None, f'Javbus 상세페이지 HTTP {r.status}'

        s = _soup(r.text)

        # 연령확인 페이지 감지 → 확인 링크 클릭 후 재시도
        is_age_gate = (
            s.select_one('#age-check, .age-check, form[action*="age"]') or
            'Age Verification' in r.text or
            ('연령' in (s.title.string or '') if s.title else False)
        )
        if is_age_gate:
            print(f'[Javbus] 연령확인 페이지 감지 → 확인 링크 추적', flush=True)
            # "ENTER" 또는 "YES" 등 확인 링크 찾기
            confirm = (
                s.select_one('a[href*="age"], a.btn-default, '
                             'a[href*="confirm"], a[href*="enter"]')
            )
            if confirm:
                href = confirm.get('href', '')
                if href and not href.startswith('http'):
                    href = JAVBUS_BASE + href
                if href:
                    print(f'[Javbus] age-confirm → {href}', flush=True)
                    time.sleep(0.5)
                    r2 = _get(href, referer=url_direct, cookies=_JAVBUS_COOKIES)
                    # 확인 후 원래 URL 재요청
                    time.sleep(0.5)
                    r = _get(url_direct, referer=href, cookies=_JAVBUS_COOKIES)
                    s = _soup(r.text)
            # 여전히 age gate면 실패
            if 'Age Verification' in r.text:
                print(f'[Javbus] 연령확인 우회 실패', flush=True)
                return None, 'Javbus 연령확인 우회 실패 (VPN 필요)'

        result = {'code': code, 'source': 'javbus'}

        # 제목: h3 → .title h3 → og:title 순으로 시도
        title = ''
        for sel in ('h3', '.title h3', 'h2.title', '.video-title'):
            el = s.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                # 코드 자체가 제목에 들어있으면 제거
                title = title.replace(code, '').replace(code.replace('-',''), '').strip()
                if title:
                    break
        if not title:
            meta_og = s.find('meta', property='og:title')
            if meta_og:
                title = meta_og.get('content', '').replace(code, '').strip()

        # 파싱 진단
        print(f'[Javbus] HTML 길이={len(r.text)}  title태그="{s.title.string if s.title else ""}"', flush=True)
        result['title'] = title

        actresses, genres = [], []
        studio = date = ''

        for p in s.select('.info p, .movie-info p'):
            txt = p.get_text()
            links = [a.get_text(strip=True) for a in p.select('a')]
            if any(k in txt for k in ('出演', '女優', '演員', 'Cast', '출연')):
                actresses = [l for l in links if l]
            elif any(k in txt for k in ('ジャンル', 'Genre', '類別', '장르')):
                genres = [l for l in links if l]
            elif any(k in txt for k in ('スタジオ', 'Studio', '片商', 'Maker')):
                studio = links[0] if links else ''
            elif any(k in txt for k in ('発売日', 'Date', '日期', 'Release')):
                span = p.select_one('span')
                date = span.get_text(strip=True) if span else ''

        # 배우 별도 섹션 (여러 셀렉터 시도)
        if not actresses:
            for sel in ('.star-show .avatar-box', '.actress-box .box',
                        '.star-box .actress-avatar', '[class*="actress"]'):
                for box in s.select(sel):
                    nm = box.select_one('span, p')
                    if nm:
                        actresses.append(nm.get_text(strip=True))
                if actresses:
                    break

        cover = s.select_one('.screencap img, .bigImage img, .video-cover img')
        result['actresses'] = actresses
        result['genres']    = genres
        result['studio']    = studio
        result['date']      = date
        result['cover_url'] = (cover.get('src') or cover.get('data-src', '')) if cover else ''

        print(f'[Javbus] 제목="{result["title"]}" 배우={actresses[:2]}', flush=True)
        if result['title']:
            return result, ''
        return None, 'Javbus 제목 파싱 실패 (HTML 구조 변경 가능성)'

    except Exception as e:
        import traceback
        msg = f'Javbus 예외: {e}'
        print(f'[jav_scraper] {msg}\n{traceback.format_exc()}', flush=True)
        return None, msg

# ─────────────────────────────────────────────────
#  오프라인 JSON 덤프
#  파일명: jav_offline.json  (스크립트와 같은 폴더)
# ─────────────────────────────────────────────────
_OFFLINE_DB: dict = {}
_OFFLINE_LOADED: bool = False

def _load_offline():
    global _OFFLINE_DB, _OFFLINE_LOADED
    if _OFFLINE_LOADED:
        return
    _OFFLINE_LOADED = True
    candidates = [
        Path(__file__).parent / 'jav_offline.json',
        Path('jav_offline.json'),
    ]
    for p in candidates:
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding='utf-8'))
                # 키를 대문자+하이픈 정규화
                for k, v in raw.items():
                    norm = _normalize_key(k)
                    _OFFLINE_DB[norm] = v
                print(f'[jav_scraper] 오프라인 DB 로드: {p}  ({len(_OFFLINE_DB)}개)', flush=True)
                return
            except Exception as e:
                print(f'[jav_scraper] 오프라인 DB 로드 실패: {e}', flush=True)
    print('[jav_scraper] jav_offline.json 없음 → 라이브 스크래핑만 사용', flush=True)

def _normalize_key(code: str) -> str:
    """코드를 대문자+하이픈 형식으로 정규화  예) pred123 → PRED-123"""
    code = code.upper().strip()
    m = re.match(r'([A-Z]+)-?(\d+)', code)
    if not m:
        return code
    return f'{m.group(1)}-{m.group(2).zfill(3)}'

def _lookup_offline(code: str) -> tuple:
    """오프라인 DB에서 조회. (meta | None, error_str)"""
    _load_offline()
    if not _OFFLINE_DB:
        return None, '오프라인 DB 없음'
    norm = _normalize_key(code)
    row  = _OFFLINE_DB.get(norm)
    if not row:
        return None, f'오프라인 DB에 {norm} 없음'
    meta = {
        'code':      norm,
        'source':    'offline',
        'title':     row.get('title', ''),
        'actresses': row.get('actresses', []),
        'genres':    row.get('genres', []),
        'studio':    row.get('studio', ''),
        'date':      row.get('date', ''),
        'cover_url': row.get('cover_url', ''),
    }
    if not meta['title']:
        return None, f'오프라인 DB에 {norm} 있으나 title 없음'
    print(f'[offline] HIT {norm}: {meta["title"]}', flush=True)
    return meta, ''

# ─────────────────────────────────────────────────
#  R18.dev  (FANZA 공식 JSON API — 스크래핑 없음)
# ─────────────────────────────────────────────────
R18_BASE = 'https://r18.dev/videos/vod/movies/detail/-/dvd_id={}/json/'
_R18_HEADERS = {
    'User-Agent':      _HEADERS['User-Agent'],
    'Accept':          'application/json, text/plain, */*',
    'Accept-Language': 'ja,ko;q=0.9,en;q=0.8',
    'Referer':         'https://r18.dev/',
    'Origin':          'https://r18.dev',
}

def _fetch_r18(code: str) -> tuple:
    """(meta_dict | None, error_str) 반환.
    R18.dev 공식 JSON API — HTML 파싱 없음."""
    try:
        url = R18_BASE.format(code)
        print(f'[R18.dev] GET {url}', flush=True)

        if _HAS_CFFI:
            resp = _cffi_requests.get(url, headers=_R18_HEADERS,
                                      impersonate='chrome110',
                                      timeout=15, verify=False)
            status = resp.status_code
            if status != 200:
                print(f'[R18.dev] HTTP {status}', flush=True)
                return None, f'R18.dev HTTP {status}'
            data = resp.json()
        elif _HAS_HTTPX:
            with httpx.Client(timeout=15, follow_redirects=True, verify=False) as c:
                resp = c.get(url, headers=_R18_HEADERS)
            status = resp.status_code
            if status != 200:
                print(f'[R18.dev] HTTP {status}', flush=True)
                return None, f'R18.dev HTTP {status}'
            data = resp.json()
        else:
            import urllib.request, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                        'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                status = r.status
                if status != 200:
                    return None, f'R18.dev HTTP {status}'
                data = json.loads(r.read().decode('utf-8'))

        # ── 파싱 ──
        # 배우: name_romaji(영문) 또는 name(일본어) 사용
        actresses = []
        for a in (data.get('actresses') or []):
            name = (a.get('name_romaji') or a.get('name') or '').strip()
            if name:
                actresses.append(name)

        # 장르/카테고리
        genres = []
        for c in (data.get('categories') or []):
            g = (c.get('name') or '').strip()
            if g:
                genres.append(g)

        # 제목: title_ja(일본어 원제) 우선, 없으면 title(영문)
        title = (data.get('title_ja') or data.get('title') or '').strip()

        # 스튜디오/메이커
        maker = data.get('maker') or {}
        studio = (maker.get('name') or '').strip()

        # 커버 이미지
        images = data.get('images') or {}
        jacket = images.get('jacket_image') or {}
        cover_url = jacket.get('large') or jacket.get('small') or ''

        release_date = (data.get('release_date') or '')[:10]

        print(f'[R18.dev] 제목="{title}" 배우={actresses[:3]} 장르={genres[:3]}', flush=True)

        if not title:
            return None, 'R18.dev 응답에 제목 없음 (미등록 코드)'

        return {
            'code':      code,
            'source':    'r18dev',
            'title':     title,
            'actresses': actresses,
            'genres':    genres,
            'studio':    studio,
            'date':      release_date,
            'cover_url': cover_url,
        }, ''

    except Exception as e:
        import traceback
        msg = f'R18.dev 예외: {e}'
        print(f'[jav_scraper] {msg}\n{traceback.format_exc()}', flush=True)
        return None, msg

# ─────────────────────────────────────────────────
#  FC2DB (fc2db.net) — FC2-PPV 전용 스크래퍼
# ─────────────────────────────────────────────────
FC2DB_BASE = 'https://fc2db.net'

def _fetch_fc2db(code: str) -> tuple:
    """FC2-PPV 코드 → fc2db.net 스크래핑.
    (meta_dict | None, error_str) 반환

    URL 패턴: FC2-PPV-4876937 → https://fc2db.net/work/4876937/
    """
    if not _HAS_BS4:
        return None, 'beautifulsoup4 미설치'

    # FC2-PPV-XXXXXXX 에서 숫자 추출
    m = re.search(r'FC2-PPV-(\d+)', code.upper())
    if not m:
        return None, f'FC2-PPV 코드 형식 오류: {code}'
    number = m.group(1)

    url = f'{FC2DB_BASE}/work/{number}/'
    print(f'[FC2DB] GET {url}', flush=True)
    r = _get(url)
    print(f'[FC2DB] status={r.status}  len={len(r.text)}', flush=True)
    if r.status not in (200, 0):
        return None, f'FC2DB HTTP {r.status}'
    # 소프트 404 감지: HTML이 5KB 미만이면 미등록 페이지
    if len(r.text) < 5000:
        return None, f'FC2DB 미등록 (응답 {len(r.text)}B — DB에 없는 ID)'

    s = _soup(r.text)

    # ── JSON-LD 빠른 파싱 ──
    title = ''
    cover_url = ''
    date = ''
    for script in s.find_all('script', type='application/ld+json'):
        try:
            ld = json.loads(script.string or '')
            # 단일 객체
            if isinstance(ld, dict):
                items = ld.get('@graph', [ld])
                for item in items:
                    if isinstance(item, dict) and item.get('@type') in (
                            'VideoObject', 'Movie', 'Product'):
                        title     = (item.get('name') or '').strip()
                        cover_url = item.get('thumbnailUrl', '')
                        date      = (item.get('uploadDate') or
                                     item.get('datePublished') or '')[:10]
                        if title:
                            break
            if title:
                break
        except Exception:
            pass

    # ── HTML 보완 파싱 ──
    if not title:
        # <h2 class="... font-extrabold ..."> 내부 <a>
        for sel in ('h2.font-extrabold a', 'h2.text-xl a',
                    'h1.font-extrabold a', 'h1 a', 'h2 a'):
            el = s.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                if title:
                    break

    if not title:
        return None, f'FC2DB에서 {code} 제목 파싱 실패 (HTML 구조 변경 가능성)'

    # ── 태그 파싱 ──
    # <a href="...work-tags..."> 또는 <a href="...tag...">
    tags = []
    seen = set()
    for a in s.select('a[href*="work-tags"], a[href*="/tag/"]'):
        tag_text = a.get_text(strip=True)
        if tag_text and tag_text not in seen:
            tags.append(tag_text)
            seen.add(tag_text)

    # ── 커버 이미지 ──
    if not cover_url:
        for sel in ('img.wp-post-image', 'article img[src*="fc2db"]',
                    '.wp-post-image', 'article img', 'figure img'):
            img = s.select_one(sel)
            if img:
                src = img.get('src') or img.get('data-src', '')
                if src and ('fc2db' in src or 'img.' in src):
                    cover_url = src
                    break

    # ── 판매자 (채널/셀러) ──
    seller = ''
    for sel in ('a[href*="/seller/"]', 'a[href*="/channel/"]',
                'a[href*="/maker/"]'):
        a_el = s.select_one(sel)
        if a_el:
            sp = a_el.select_one('span.font-medium, span')
            seller = sp.get_text(strip=True) if sp else a_el.get_text(strip=True)
            if seller:
                break

    # ── dt/dd 그리드: 販売日, 収録時間 ──
    if not date:
        for dt in s.select('dt'):
            if '販売日' in dt.get_text():
                dd = dt.find_next_sibling('dd')
                if dd:
                    date = dd.get_text(strip=True)
                break

    duration_str = ''
    for dt in s.select('dt'):
        if '収録時間' in dt.get_text():
            dd = dt.find_next_sibling('dd')
            if dd:
                duration_str = dd.get_text(strip=True)
            break

    genres = ['FC2', 'FC2-PPV', '아마추어'] + tags

    result = {
        'code':         code.upper(),
        'source':       'fc2db',
        'title':        title,
        'actresses':    [],
        'genres':       genres,
        'studio':       seller,
        'date':         date,
        'cover_url':    cover_url,
        'duration_str': duration_str,
    }
    print(f'[FC2DB] 제목="{title}" 태그={tags[:5]} 판매자="{seller}"', flush=True)
    return result, ''


# ─────────────────────────────────────────────────
#  FC2PPVDB (fc2ppvdb.com) — 로그인 필요, 가장 풍부한 메타
# ─────────────────────────────────────────────────
FC2PPVDB_BASE    = 'https://fc2ppvdb.com/public'
_ppvdb_sess      = None
_ppvdb_logged_in = False
_ppvdb_email     = ''
_ppvdb_password  = ''

# 🔥 테스트 코드와 100% 동일한 순수 헤더 (전역 _HEADERS 오염 방지)
_PPVDB_HDRS_BASE = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'ja,ko;q=0.9,en-US;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

class _PPVDBSession:
    """테스트 코드의 Session 클래스를 그대로 이식한 독립 세션"""
    def __init__(self):
        if _HAS_CFFI:
            from curl_cffi.requests import Session as _CSession
            self._s = _CSession(impersonate='chrome110', verify=False)
            self.engine = 'cffi'
        elif _HAS_HTTPX:
            self._s = httpx.Client(follow_redirects=True, verify=False, timeout=20)
            self.engine = 'httpx'
        else:
            import http.cookiejar as _cj, ssl as _ssl, urllib.request as _ur
            jar = _cj.CookieJar()
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False; ctx.verify_mode = _ssl.CERT_NONE
            self._s = _ur.build_opener(_ur.HTTPCookieProcessor(jar), _ur.HTTPSHandler(context=ctx))
            self.engine = 'urllib'

    def get(self, url, headers=None):
        h = {**_PPVDB_HDRS_BASE, 'Accept': 'text/html,*/*', **(headers or {})}
        if self.engine == 'cffi':
            r = self._s.get(url, headers=h, timeout=20)
            return r.status_code, r.text
        elif self.engine == 'httpx':
            r = self._s.get(url, headers=h)
            return r.status_code, r.text
        else:
            import urllib.request as _ur, urllib.error as _ue
            req = _ur.Request(url, headers=h)
            try:
                with self._s.open(req, timeout=20) as r:
                    return r.status, r.read().decode('utf-8', errors='replace')
            except _ue.HTTPError as e:
                return e.code, e.read().decode('utf-8', errors='replace')
            except Exception:
                return 0, ''

    def post(self, url, data: dict, headers=None):
        h = {**_PPVDB_HDRS_BASE,
             'Content-Type': 'application/x-www-form-urlencoded',
             'Origin': 'https://fc2ppvdb.com',
             'Referer': f'{FC2PPVDB_BASE}/login',
             **(headers or {})}
        if self.engine == 'cffi':
            r = self._s.post(url, data=data, headers=h, timeout=20)
            return r.status_code, r.text
        elif self.engine == 'httpx':
            r = self._s.post(url, data=data, headers=h)
            return r.status_code, r.text
        else:
            import urllib.request as _ur, urllib.error as _ue
            encoded = urllib.parse.urlencode(data).encode()
            req = _ur.Request(url, data=encoded, headers=h)
            try:
                with self._s.open(req, timeout=20) as r:
                    return r.status, r.read().decode('utf-8', errors='replace')
            except _ue.HTTPError as e:
                return e.code, e.read().decode('utf-8', errors='replace')
            except Exception:
                return 0, ''


def set_fc2ppvdb_credentials(email: str, password: str):
    global _ppvdb_email, _ppvdb_password, _ppvdb_logged_in
    if email != _ppvdb_email or password != _ppvdb_password:
        _ppvdb_logged_in = False
    _ppvdb_email    = email
    _ppvdb_password = password


def _ppvdb_login() -> bool:
    global _ppvdb_sess, _ppvdb_logged_in
    _ppvdb_logged_in = False

    if not _ppvdb_email or not _ppvdb_password:
        return False

    print(f'[FC2PPVDB] 로그인 시도 ({_ppvdb_email})', flush=True)
    try:
        # 매 로그인 시도마다 깨끗한 독립 세션 생성
        _ppvdb_sess = _PPVDBSession()
        
        status, html = _ppvdb_sess.get(f'{FC2PPVDB_BASE}/login')
        if status != 200 or not html:
            print(f'[FC2PPVDB] 로그인 페이지 실패 (status={status})', flush=True)
            return False
            
        csrf = ''
        for pat in [r'<meta name="csrf-token" content="([^"]+)"',
                    r'name="_token"[^>]+value="([^"]+)"',
                    r'"csrfToken":"([^"]+)"']:
            m = re.search(pat, html)
            if m:
                csrf = m.group(1); break
                
        if not csrf:
            print('[FC2PPVDB] CSRF 토큰 없음 (Cloudflare 차단?)', flush=True)
            return False

        status2, html2 = _ppvdb_sess.post(f'{FC2PPVDB_BASE}/login', {
            '_token': csrf, 'email': _ppvdb_email, 'password': _ppvdb_password
        })
        
        if 'ログアウト' in html2 or 'logout' in html2.lower():
            _ppvdb_logged_in = True
            print('[FC2PPVDB] 로그인 성공', flush=True)
            return True
            
        print(f'[FC2PPVDB] 로그인 실패 (status={status2})', flush=True)
        return False
        
    except Exception as e:
        print(f'[FC2PPVDB] 로그인 예외: {e}', flush=True)
        return False


def _fetch_fc2ppvdb(code: str) -> tuple:
    global _ppvdb_logged_in

    if not _ppvdb_email:
        return None, 'FC2PPVDB 계정 미설정'

    m = re.search(r'FC2-PPV-(\d+)', code.upper())
    if not m:
        return None, f'FC2-PPV 코드 형식 오류: {code}'
    number = m.group(1)

    if not _ppvdb_logged_in or _ppvdb_sess is None:
        if not _ppvdb_login():
            return None, 'FC2PPVDB 로그인 실패'

    # 🔥 [핵심 비법] 테스트 코드(STEP 3)처럼 API 호출 전 일반 페이지 먼저 방문!
    # 서버에 '나 브라우저로 접속한 사람이야'라고 흔적(쿠키/로그)을 남깁니다.
    html_url = f'{FC2PPVDB_BASE}/articles/{number}'
    print(f'[FC2PPVDB] GET {html_url} (사전 방문)', flush=True)
    try:
        _ppvdb_sess.get(html_url, headers={'Referer': f'{FC2PPVDB_BASE}/'})
        time.sleep(random.uniform(0.5, 1.5))  # 사람처럼 0.5~1.5초 대기
    except Exception:
        pass

    # 이제 안심하고 원본 API 주소 호출 (테스트 코드 STEP 5)
    url = f'{FC2PPVDB_BASE}/articles/article-info?videoid={number}'
    print(f'[FC2PPVDB] GET {url} (API 호출)', flush=True)

    try:
        status, text = _ppvdb_sess.get(url, headers={
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': html_url,
        })
    except Exception as e:
        return None, f'FC2PPVDB 요청 예외: {e}'

    print(f'[FC2PPVDB] status={status}  len={len(text)}', flush=True)
    if status != 200:
        return None, f'FC2PPVDB HTTP {status}'

    if text.strip().startswith('{') or text.strip().startswith('['):
        try:
            data = json.loads(text)
        except Exception:
            return None, 'FC2PPVDB JSON 파싱 실패'
    else:
        print(f'[FC2PPVDB] 응답이 JSON이 아닙니다. 앞부분: {text[:200]}...', flush=True)
        return None, 'FC2PPVDB 응답 오류'

    if data.get('isLoggedIn', 1) == 0:
        print('[FC2PPVDB] 세션 만료 → 재로그인', flush=True)
        _ppvdb_logged_in = False
        if not _ppvdb_login():
            return None, 'FC2PPVDB 세션 만료 후 재로그인 실패'
        return _fetch_fc2ppvdb(code)

    art = data.get('article') or {}
    if not art:
        return None, f'FC2PPVDB: {code} article 없음'

    title = (art.get('title') or '').strip()
    if not title:
        return None, f'FC2PPVDB: {code} 제목 없음'

    tags       = [t['name'] for t in (art.get('tags') or []) if t.get('name')]
    actresses  = [a['name'] for a in (art.get('actresses') or []) if a.get('name')]
    writer     = (art.get('writer') or {}).get('name', '')
    image_url  = art.get('image_url', '') or ''
    if image_url.startswith('/'):
        image_url = f'https://fc2ppvdb.com{image_url}'
    if 'no-image' in image_url:
        image_url = ''

    result = {
        'code':         code.upper(),
        'source':       'fc2ppvdb',
        'title':        title,
        'actresses':    actresses,
        'genres':       ['FC2', 'FC2-PPV', '아마추어'] + tags,
        'studio':       writer,
        'date':         (art.get('release_date') or '')[:10],
        'cover_url':    image_url,
        'duration_str': art.get('duration', '') or '',
    }
    print(f'[FC2PPVDB] 제목="{title}" 배우={actresses[:2]} 태그={tags[:5]}', flush=True)
    return result, ''

# ─────────────────────────────────────────────────
#  공개 API
# ─────────────────────────────────────────────────
def fetch_meta(code: str) -> dict | None:
    """오프라인 → FC2DB(FC2전용) → R18.dev → JavDB → Javbus 순서로 조회. 실패 시 None."""
    meta, _ = fetch_meta_verbose(code)
    return meta

def fetch_meta_verbose(code: str) -> tuple:
    """(meta_dict | None, error_str). 최종 실패 원인 포함.

    조회 순서:
      0-a) FC2-PPV → fc2db.net 스크래핑 (로그인 불필요)
      0-b) FC2-PPV → fc2ppvdb.com API (로그인 필요, 가장 풍부)
      0-c) FC2-PPV → fallback 최소 메타
      1) 오프라인 JSON  (jav_offline.json)
      2) R18.dev JSON API  (FANZA 공식)
      3) JavDB 스크래핑
      4) Javbus 스크래핑
    """
    # 0) FC2-PPV 전용 처리
    if code.upper().startswith('FC2-PPV-'):
        # 0-a) fc2db.net (로그인 불필요)
        meta, err_fc2db = _fetch_fc2db(code)
        if meta:
            return meta, ''
        print(f'[FC2DB] {err_fc2db}', flush=True)

        # 0-b) fc2ppvdb.com API (계정 설정 시)
        if _ppvdb_email:
            meta, err_ppvdb = _fetch_fc2ppvdb(code)
            if meta:
                return meta, ''
            print(f'[FC2PPVDB] {err_ppvdb}', flush=True)

        # 0-c) fallback
        return {
            'code':      code.upper(),
            'title':     code.upper(),
            'actresses': [],
            'genres':    ['FC2', 'FC2-PPV', '아마추어'],
            'source':    'fc2-fallback',
        }, ''

    # 1) 오프라인 덤프
    meta, err0 = _lookup_offline(code)
    if meta:
        return meta, ''

    # 2) R18.dev 공식 API
    meta, err1 = _fetch_r18(code)
    if meta:
        return meta, ''

    # 3) JavDB 스크래핑
    meta, err2 = _fetch_javdb(code)
    if meta:
        return meta, ''

    # 4) Javbus 스크래핑
    meta, err3 = _fetch_javbus(code)
    if meta:
        return meta, ''

    combined = (f'오프라인: {err0} / R18: {err1} / '
                f'JavDB: {err2} / Javbus: {err3}')
    return None, combined

def offline_db_stats() -> str:
    """오프라인 DB 현황 문자열 반환 (UI 표시용)"""
    _load_offline()
    if _OFFLINE_DB:
        return f'오프라인 DB: {len(_OFFLINE_DB):,}개 코드'
    return '오프라인 DB: 없음 (jav_offline.json 배치 시 우선 사용)'

def fetch_javdatabase_info(slug: str) -> tuple:
    """javdatabase.com/idols/{slug}/ 에서 JAV 배우 프로필 스크래핑.
    slug: 'rei-saegusa' 형식 (소문자, 하이픈 구분)
    반환: (raw_text: str, error_msg: str)"""
    if not _HAS_BS4:
        return '', 'beautifulsoup4 미설치'
    url = f'https://www.javdatabase.com/idols/{slug.lower().replace(" ", "-")}/'
    print(f'[javdatabase] GET {url}', flush=True)
    r = _get(url)
    if r.status != 200:
        return '', f'HTTP {r.status}'
    soup = _soup(r.text)
    info = {}

    # 이름 — 페이지 h1 우선
    h1 = soup.select_one('h1, .idol-name, .entry-title')
    info['이름'] = h1.get_text(strip=True) if h1 else slug

    # 프로필 컬럼 타겟: <div class="col-12 col-xxl-7 ...">
    # javdatabase.com 레이아웃: 왼쪽 이미지(col-5), 오른쪽 프로필(col-7)
    profile_root = (soup.select_one('div.col-xxl-7')
                    or soup.select_one('div.col-xl-7')
                    or soup.select_one('div.col-lg-7')
                    or soup)

    # 1. 이름 추출 (뒤에 붙는 ' - JAV Profile' 제거)
    h1 = profile_root.select_one('h1.idol-name')
    if h1:
        name_text = h1.get_text(strip=True)
        info['이름'] = name_text.replace('- JAV Profile', '').strip()

    # 2. <b> 태그를 라벨로 삼아 데이터 추출
    for b_tag in profile_root.find_all('b'):
        key = b_tag.get_text(strip=True).replace(':', '').strip()
        
        # 다음 <b>나 <br>이 나오기 전까지의 형제 노드(텍스트, a 태그)를 모두 합침
        val = ''
        node = b_tag.next_sibling
        while node and node.name not in ('b', 'br'):
            if isinstance(node, str):
                val += node
            elif node.name == 'a':
                val += node.get_text(strip=True)
            node = node.next_sibling
        
        # 앞뒤 불필요한 하이픈이나 공백 제거
        val = val.strip(' -')
        
        if not val or val == '?':
            continue
            
        kl = key.lower()
        if 'dob' in kl or 'birth' in kl:
            info['생년월일'] = val
        elif 'age' == kl:
            info['나이'] = val
        elif 'height' in kl:
            info['신장'] = val
        elif 'measurements' in kl:
            info['체형'] = val
        elif 'cup' in kl:
            info['컵'] = val
        elif 'debut' == kl:
            info['데뷔'] = val
        elif 'jp' in kl:
            info['일본어이름'] = val

    # 3. 태그 파싱 (Tags: 바로 뒤의 a 태그들 수집)
    tags_b = profile_root.find('b', string=lambda text: text and 'Tags:' in text)
    if tags_b:
        tags = []
        node = tags_b.next_sibling
        while node and node.name != 'br':
            if node.name == 'a' and 'Suggest' not in node.get_text():
                tags.append(node.get_text(strip=True))
            node = node.next_sibling
        if tags:
            info['태그'] = ', '.join(tags)

    # 이미지 — 프로필 컬럼 왼쪽 col (이미지 컬럼) 또는 전체에서 탐색
    img_root = (soup.select_one('div.col-xxl-5')
                or soup.select_one('div.col-xl-5')
                or soup)
    img = img_root.select_one(
        'img[src*="idolimages"], img[src*="idol"], '
        '.idol-image img, .idol-photo img, .profile-image img, article img'
    )
    if img and img.get('src'):
        info['image_url'] = img['src']

    lines = [f"이름: {info.get('이름', slug)}"]
    lines += [f'{k}: {v}' for k, v in info.items() if k not in ('이름', 'image_url')]
    if 'image_url' in info:
        lines.append(f"image_url: {info['image_url']}")
    return '\n'.join(lines), ''


def fetch_babepedia_info(slug: str) -> tuple:
    """babepedia.com/babe/{slug} 에서 서양 배우 프로필 스크래핑.
    slug: 'Danni_Ashe' 형식 (언더바 구분, 대소문자 유지)
    반환: (raw_text: str, error_msg: str)"""
    if not _HAS_BS4:
        return '', 'beautifulsoup4 미설치'
    url = f'https://www.babepedia.com/babe/{slug}'
    print(f'[babepedia] GET {url}', flush=True)
    r = _get(url)
    if r.status != 200:
        return '', f'HTTP {r.status}'
    soup = _soup(r.text)
    info = {}

    h1 = soup.select_one('h1, .babe-name, .model-name')
    info['이름'] = h1.get_text(strip=True) if h1 else slug.replace('_', ' ')

    # 소개 텍스트 (biotext)
    biotext = soup.select_one('#biotext, p#biotext')
    if biotext:
        bt = biotext.get_text(' ', strip=True)
        if bt:
            info['소개'] = bt

    # 프로필 블록: #personal-info-block 우선, 없으면 전체
    info_root = soup.select_one('#personal-info-block') or soup

    # 프로필 ul/li, table, 또는 div 구조
    profile_items = info_root.select(
        'li, table tr, .biodata li, .profile li, .bio li, '
        '.model-info li, .info-list li, .babe-details li'
    )
    for item in profile_items:
        # "Label: Value" 형태로 분리
        txt = item.get_text(separator=':', strip=True)
        if ':' not in txt:
            continue
        key, _, val = txt.partition(':')
        key = key.strip()
        val = val.strip()
        if not val or val in ('-', 'N/A', 'Unknown', 'n/a', '?'):
            continue
        kl = key.lower()
        if any(k in kl for k in ['born', 'birthday', 'birth date', 'date of birth']):
            info['생년월일'] = val
        elif any(k in kl for k in ['age', '나이']):
            info['나이'] = val
        elif 'height' in kl:
            info['신장'] = val
        elif 'weight' in kl:
            info['체중'] = val
        elif any(k in kl for k in ['measure', 'bust', 'waist', 'hip', 'size']):
            info.setdefault('체형', val)
        elif 'nation' in kl or 'ethnic' in kl or 'country' in kl:
            info['국적'] = val
        elif any(k in kl for k in ['career', 'active', 'years active']):
            info['활동기간'] = val
        elif 'hair' in kl:
            info['헤어'] = val
        elif 'eye' in kl:
            info['눈색'] = val
        elif any(k in kl for k in ['alias', 'also known', 'aka']):
            info['별명'] = val

    # 태그 / 카테고리
    tag_links = soup.select(
        'a[href*="/tag/"], a[href*="/category/"], a[href*="/tags/"], '
        '.tags a, .categories a, .model-tags a, .babe-tags a'
    )
    tags = list(dict.fromkeys(
        a.get_text(strip=True) for a in tag_links
        if a.get_text(strip=True) and len(a.get_text(strip=True)) > 1
    ))[:20]
    if tags:
        info['태그'] = ', '.join(tags)

    # 이미지
    img = soup.select_one(
        '.babe-image img, .profile-image img, .model-photo img, '
        '.babe-photo img, article img, .main-image img'
    )
    if img and img.get('src'):
        info['image_url'] = img['src']

    lines = [f"이름: {info.get('이름', slug)}"]
    lines += [f'{k}: {v}' for k, v in info.items() if k not in ('이름', 'image_url')]
    if 'image_url' in info:
        lines.append(f"image_url: {info['image_url']}")
    return '\n'.join(lines), ''


def fetch_actress_info(name: str) -> tuple:
    """배우 이름으로 JavDB에서 프로필 정보 검색.
    반환: (raw_text_kr: str, error_msg: str)
    실패 시 ('', 에러내용) 반환."""
    if not _HAS_BS4:
        return '', 'beautifulsoup4 미설치'
    try:
        import urllib.parse as _up
        url = f'{JAVDB_BASE}/actors?q={_up.quote(name)}&f=actor'
        print(f'[배우검색] GET {url}', flush=True)
        r = _get(url)
        if r.status != 200:
            return '', f'JavDB 배우 검색 HTTP {r.status}'
        soup = _soup(r.text)

        # 첫 번째 배우 카드 링크 추출
        card = soup.select_one('.actor-box a, .box-actor a')
        if not card:
            return '', f'"{name}" 검색 결과 없음'

        actor_href = card.get('href', '')
        if not actor_href:
            return '', '배우 링크 없음'

        actor_url = f'{JAVDB_BASE}{actor_href}'
        print(f'[배우검색] 프로필 GET {actor_url}', flush=True)
        r2 = _get(actor_url)
        if r2.status != 200:
            return '', f'배우 프로필 HTTP {r2.status}'

        soup2 = _soup(r2.text)
        info = {}

        # 이름 (검색한 이름 우선, 페이지에서 보완)
        h_name = soup2.select_one('h2.title, .actor-name, h1')
        info['이름'] = h_name.get_text(strip=True) if h_name else name

        # 썸네일 이미지
        img = soup2.select_one('.avatar-box img, .actor-image img, .video-img-box img')
        info['image_url'] = img['src'] if img and img.get('src') else ''

        # 프로필 패널 — JavDB는 <nav class="panel-block"> 또는 <div class="panel-block">
        for block in soup2.select('.panel-block, .data-item'):
            strong = block.select_one('strong')
            if not strong:
                continue
            key = strong.get_text(strip=True).rstrip(':：')
            val = block.get_text(strip=True)
            # 키 텍스트 제거
            for _k in [key, key + ':', key + '：']:
                val = val.replace(_k, '', 1)
            val = val.strip()
            if not val:
                continue
            if any(k in key for k in ['生日', 'Born', 'Birthday', '誕生']):
                info['생년월일'] = val
            elif any(k in key for k in ['身高', 'Height', '身長']):
                info['신장'] = val
            elif 'Cup' in key or 'カップ' in key:
                info['컵'] = val
            elif any(k in key for k in ['國籍', 'Nationality', '国籍']):
                info['국적'] = val
            elif any(k in key for k in ['出道', 'Debut', 'デビュー']):
                info['데뷔'] = val
            elif any(k in key for k in ['作品', 'Videos', '작품수']):
                info['작품수'] = val

        # 텍스트 조합 (image_url은 별도 보관)
        image_url = info.pop('image_url', '')
        lines = [f'{k}: {v}' for k, v in info.items() if k != '이름']
        result_text = f"이름: {info.get('이름', name)}\n" + '\n'.join(lines)
        if image_url:
            result_text += f'\nimage_url: {image_url}'
        return result_text, ''

    except Exception as e:
        print(f'[배우검색] 오류: {e}', flush=True)
        return '', str(e)


def fetch_namuwiki_info(name: str) -> tuple:
    """나무위키에서 배우/인물 정보 수집.
    name: 배우명 (한국어/일본어/영어 모두 가능)
    반환: (raw_text: str, error_msg: str)"""
    if not _HAS_BS4:
        return '', 'beautifulsoup4 미설치'
    import urllib.parse as _up
    url = 'https://namu.wiki/w/' + _up.quote(name, safe='')
    print(f'[나무위키] GET {url}', flush=True)
    r = _get(url, timeout=15)
    if r.status != 200:
        return '', f'HTTP {r.status}'
    if len(r.text) < 1000:
        return '', '페이지 없음 또는 차단됨'

    soup = _soup(r.text)

    # 페이지 제목
    title_el = soup.select_one('h1, .title, [class*="documentTitle"]')
    title = title_el.get_text(strip=True) if title_el else name

    # "상세" 섹션만 추출
    # 나무위키 구조: <div class="wiki-heading"> 또는 <h2/h3/h4>로 섹션 구분
    target_keywords = ('상세', '프로필', '인물 정보', '기본 정보', 'profile')
    content_parts = []
    found_section = False

    # 모든 직계 자식 순회 (섹션 경계 감지)
    content_root = soup.select_one('[class*="wiki-content"], #article-content, article, main')
    if not content_root:
        content_root = soup.body

    if content_root:
        for el in content_root.descendants:
            if el.name in ('h2', 'h3', 'h4') or (
                    hasattr(el, 'get') and 'heading' in ' '.join(el.get('class') or [])):
                txt = el.get_text(strip=True)
                if any(kw in txt for kw in target_keywords):
                    found_section = True
                    continue
                elif found_section:
                    break
            if found_section and el.name in ('p', 'li', 'td', 'dd', 'span', 'div'):
                t = el.get_text(' ', strip=True)
                if t and len(t) > 5:
                    content_parts.append(t)

    # fallback: 섹션 못 찾으면 페이지 앞부분 텍스트
    if not content_parts:
        if content_root:
            raw_text = content_root.get_text(' ', strip=True)
            content_parts = [raw_text[:800]]

    text = f'이름: {title}\n' + '\n'.join(content_parts)
    # 중복 줄 제거 후 2000자 제한
    seen, deduped = set(), []
    for line in text.splitlines():
        ls = line.strip()
        if ls and ls not in seen:
            seen.add(ls)
            deduped.append(ls)
    return '\n'.join(deduped)[:2000], ''


def check_deps() -> list[str]:
    """누락 의존성 목록 반환 (필수만)"""
    missing = []
    if not _HAS_HTTPX and not _HAS_CFFI:
        missing.append('httpx')
    if not _HAS_BS4:
        missing.append('beautifulsoup4')
    return missing

def scraper_engine() -> str:
    """현재 사용 중인 HTTP 엔진 이름"""
    if _HAS_CFFI:  return 'curl_cffi (Cloudflare 우회 최강)'
    if _HAS_HTTPX: return 'httpx (기본)'
    return 'urllib (내장)'

# ─────────────────────────────────────────────────
#  CLI 테스트:  python jav_scraper.py PRED-123
# ─────────────────────────────────────────────────
if __name__ == '__main__':
    import sys, json
    
    # 테스트용 임시 하드코딩 (필요시 이메일/비밀번호 변경 후 사용)
    # set_fc2ppvdb_credentials("test@email.com", "password")
    
    code_arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if not code_arg:
        print('사용법: python jav_scraper.py <AV코드>  예) python jav_scraper.py PRED-123')
        sys.exit(1)
    print(f'\n=== 테스트: {code_arg} ===\n')
    meta, err = fetch_meta_verbose(code_arg)
    if meta:
        print('\n[결과]')
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    else:
        print(f'\n[실패] {err}')