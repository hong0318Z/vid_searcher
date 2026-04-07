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

import re, time, random, json
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
    """파일명에서 AV 코드 추출.  예: SSIS-001, IPX123 → SSIS-001 / IPX-123"""
    stem = Path(filename).stem.upper()
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

        # 연령확인 페이지 감지
        if s.select_one('#age-check, .age-check, form[action*="age"]') or \
           '연령' in (s.title.string or '') if s.title else False:
            print(f'[Javbus] 연령확인 페이지 감지됨 (쿠키 미적용)', flush=True)
            return None, 'Javbus 연령확인 페이지 (over18 쿠키 미적용)'

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
        return None, 'Javbus 제목 파싱 실패'

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
#  공개 API
# ─────────────────────────────────────────────────
def fetch_meta(code: str) -> dict | None:
    """오프라인 → R18.dev → JavDB → Javbus 순서로 조회. 실패 시 None."""
    meta, _ = fetch_meta_verbose(code)
    return meta

def fetch_meta_verbose(code: str) -> tuple:
    """(meta_dict | None, error_str). 최종 실패 원인 포함.

    조회 순서:
      1) 오프라인 JSON  (jav_offline.json)
      2) R18.dev JSON API  (FANZA 공식, 빠르고 안정적)
      3) JavDB 스크래핑
      4) Javbus 스크래핑
    """
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
