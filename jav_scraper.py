"""
JavDB / Javbus 스크래퍼
AV 코드 → 제목·배우·장르·설명 조회
"""

import re, time
from pathlib import Path

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

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'ja,ko;q=0.9,en-US;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
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
#  HTTP
# ─────────────────────────────────────────────────
def _get(url: str, timeout: int = 15, cookies: dict | None = None):
    if _HAS_HTTPX:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers=_HEADERS, cookies=cookies or {}) as c:
            r = c.get(url)
            r.status = r.status_code
            return r
    import urllib.request, urllib.parse
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            class _R:
                status = resp.status
                text   = resp.read().decode('utf-8', errors='replace')
            return _R()
    except Exception as e:
        class _Err:
            status = 0
            text   = ''
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
JAVBUS_BASE = 'https://www.javbus.com'

def _fetch_javbus(code: str) -> tuple:
    """(meta_dict | None, error_str) 반환"""
    if not _HAS_BS4:
        return None, 'beautifulsoup4 미설치'
    try:
        url_direct = f'{JAVBUS_BASE}/{code}'
        print(f'[Javbus] GET {url_direct}', flush=True)
        r = _get(url_direct)
        print(f'[Javbus] status={r.status}', flush=True)
        if r.status != 200:
            # 검색 시도
            url_search = f'{JAVBUS_BASE}/search/{code}'
            print(f'[Javbus] 검색 GET {url_search}', flush=True)
            r = _get(url_search)
            print(f'[Javbus] 검색 status={r.status}', flush=True)
            if r.status != 200:
                return None, f'Javbus HTTP {r.status}'
            s    = _soup(r.text)
            a    = s.select_one('.movie-box')
            if not a:
                return None
            href = a.get('href', '')
            time.sleep(0.5)
            r    = _get(href)
            if r.status != 200:
                return None

        s = _soup(r.text)
        result = {'code': code, 'source': 'javbus'}

        title_el = s.select_one('h3')
        result['title'] = title_el.get_text(strip=True).replace(code, '').strip() \
                          if title_el else ''

        actresses, genres = [], []
        studio = date = ''

        for p in s.select('.info p'):
            txt = p.get_text()
            links = [a.get_text(strip=True) for a in p.select('a')]
            if any(k in txt for k in ('出演', '女優', '演員')):
                actresses = links
            elif any(k in txt for k in ('ジャンル', 'Genre', '類別')):
                genres = links
            elif any(k in txt for k in ('スタジオ', 'Studio', '片商')):
                studio = links[0] if links else ''
            elif any(k in txt for k in ('発売日', 'Date', '日期')):
                span = p.select_one('span')
                date = span.get_text(strip=True) if span else ''

        # 배우 별도 섹션
        if not actresses:
            for box in s.select('.star-show .avatar-box, .actress-box .box'):
                nm = box.select_one('span')
                if nm:
                    actresses.append(nm.get_text(strip=True))

        cover = s.select_one('.screencap img, .bigImage img')
        result['actresses'] = actresses
        result['genres']    = genres
        result['studio']    = studio
        result['date']      = date
        result['cover_url'] = (cover.get('src') or cover.get('data-src', '')) if cover else ''

        print(f'[Javbus] 제목="{result["title"]}" 배우={actresses}', flush=True)
        if result['title']:
            return result, ''
        return None, 'Javbus 제목 파싱 실패'

    except Exception as e:
        import traceback
        msg = f'Javbus 예외: {e}'
        print(f'[jav_scraper] {msg}\n{traceback.format_exc()}', flush=True)
        return None, msg

# ─────────────────────────────────────────────────
#  공개 API
# ─────────────────────────────────────────────────
def fetch_meta(code: str) -> dict | None:
    """JavDB → Javbus 순서로 메타데이터 조회. 실패 시 None."""
    meta, _ = fetch_meta_verbose(code)
    return meta

def fetch_meta_verbose(code: str) -> tuple:
    """(meta_dict | None, error_str) 반환. error_str은 최종 실패 원인."""
    meta, err1 = _fetch_javdb(code)
    if meta:
        return meta, ''
    meta, err2 = _fetch_javbus(code)
    if meta:
        return meta, ''
    combined = f'JavDB: {err1} / Javbus: {err2}'
    return None, combined

def check_deps() -> list[str]:
    """누락 의존성 목록 반환"""
    missing = []
    if not _HAS_HTTPX:
        missing.append('httpx')
    if not _HAS_BS4:
        missing.append('beautifulsoup4')
    return missing

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
