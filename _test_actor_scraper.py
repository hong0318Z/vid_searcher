"""
배우 스크래핑 + LLM 파이프라인 테스트 스크립트
단계별 중간 산출물을 출력해서 스크래퍼 동작과 LLM 처리 결과를 확인한다.

실행: python _test_actor_scraper.py
"""

import json, sys
from pathlib import Path

# ── 테스트 대상 설정 (여기만 수정) ──────────────────────────
TEST_NAMES = [
    "三上悠亜",     # JAV 배우 (일본어)
    "Sasha Grey",   # 서양 배우 (영어)
    # "松本いちか",  # 필요 시 추가
]
SKIP_LLM   = False  # True: LLM 호출 없이 스크래핑 결과만 출력
# ──────────────────────────────────────────────────────────────

SEP  = '━' * 60
SEP2 = '─' * 40

def banner(text):
    print(f'\n{SEP}\n{text}\n{SEP}')

def section(title, ok=True):
    mark = '✅' if ok else '❌'
    print(f'\n{SEP2}\n{mark} {title}\n{SEP2}')


# ── Config 로드 ──────────────────────────────────────────────
CFG_PATH = Path(__file__).parent / 'vidsort_cfg.json'
cfg = {}
try:
    cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
    print(f'✅ vidsort_cfg.json 로드 완료')
except Exception as e:
    print(f'⚠ vidsort_cfg.json 로드 실패: {e}')

llm_token    = cfg.get('llm_token', '')
llm_model    = cfg.get('llm_model', 'claude-sonnet-4.5')
llm_endpoint = cfg.get('llm_endpoint', 'https://api.githubcopilot.com')

# LLM 클라이언트
client = None
if not SKIP_LLM:
    if llm_token:
        try:
            from llm_api import LLMClient
            client = LLMClient(llm_token, llm_model, llm_endpoint)
            print(f'✅ LLM 클라이언트 준비 (모델: {llm_model})')
        except Exception as e:
            print(f'❌ LLM 클라이언트 초기화 실패: {e}')
    else:
        print('⚠ llm_token 없음 — LLM 단계 건너뜀')

# 스크래퍼 임포트
try:
    from jav_scraper import (
        fetch_javdatabase_info,
        fetch_babepedia_info,
        fetch_namuwiki_info,
    )
    print('✅ jav_scraper 임포트 완료')
except Exception as e:
    print(f'❌ jav_scraper 임포트 실패: {e}')
    sys.exit(1)


# ── 메인 루프 ────────────────────────────────────────────────
for name in TEST_NAMES:
    banner(f'ACTOR: {name}')

    # ── STEP 1: LLM 이름 분석 ──
    analysis_result = {}
    if client:
        section('STEP 1 — LLM analyze_actor_names()')
        try:
            result = client.analyze_actor_names([name])
            analysis_result = result.get(name, {})
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f'오류: {e}')
    else:
        section('STEP 1 — LLM 건너뜀 (SKIP_LLM=True 또는 토큰 없음)', ok=False)

    actor_type  = analysis_result.get('type', 'unknown')
    javdb_slug  = analysis_result.get('javdb_slug', '')
    babe_slug   = analysis_result.get('babepedia_slug', '')
    variants    = analysis_result.get('variants', [])

    print(f'\n  → type: {actor_type}')
    print(f'  → javdb_slug: {javdb_slug!r}')
    print(f'  → babepedia_slug: {babe_slug!r}')
    print(f'  → variants: {variants}')

    all_raw_parts = []

    # ── STEP 2-A: javdatabase.com ──
    section(f'STEP 2-A — javdatabase.com  (type={actor_type})')
    if actor_type in ('jav', 'unknown'):
        slugs_to_try = []
        if javdb_slug: slugs_to_try.append(javdb_slug)
        slugs_to_try += [v for v in variants if v and '-' in v and v not in slugs_to_try]
        if not slugs_to_try:
            print('  슬러그 없음 — 건너뜀')
        else:
            for slug in slugs_to_try[:3]:
                print(f'  시도 슬러그: {slug!r}')
                raw, err = fetch_javdatabase_info(slug)
                if raw:
                    print(f'  결과:\n{raw}')
                    all_raw_parts.append(f'[javdatabase]\n{raw}')
                    break
                else:
                    print(f'  실패: {err}')
    else:
        print('  type=western → 건너뜀')

    # ── STEP 2-B: babepedia.com ──
    section(f'STEP 2-B — babepedia.com  (type={actor_type})')
    if actor_type in ('western', 'unknown'):
        slugs_to_try = []
        if babe_slug: slugs_to_try.append(babe_slug)
        slugs_to_try += [v for v in variants if v and '_' in v and v not in slugs_to_try]
        if not slugs_to_try:
            print('  슬러그 없음 — 건너뜀')
        else:
            for slug in slugs_to_try[:3]:
                print(f'  시도 슬러그: {slug!r}')
                raw, err = fetch_babepedia_info(slug)
                if raw:
                    print(f'  결과:\n{raw}')
                    all_raw_parts.append(f'[babepedia]\n{raw}')
                    break
                else:
                    print(f'  실패: {err}')
    else:
        print('  type=jav → 건너뜀')

    # ── STEP 2-C: 나무위키 ──
    section('STEP 2-C — 나무위키 fetch_namuwiki_info()')
    namu_raw, namu_err = fetch_namuwiki_info(name)
    if namu_raw:
        print(namu_raw)
        all_raw_parts.append(f'[나무위키]\n{namu_raw}')
    else:
        print(f'결과 없음: {namu_err}')

    # ── 전체 raw 합산 출력 ──
    combined_raw = '\n\n'.join(all_raw_parts)
    print(f'\n[모든 소스 합산 raw — {len(combined_raw)}자]')
    print(combined_raw[:1000] + ('...(이하 생략)' if len(combined_raw) > 1000 else ''))

    # ── STEP 3: LLM generate_actor_info ──
    if client:
        section('STEP 3 — LLM generate_actor_info()')
        try:
            final = client.generate_actor_info(name, combined_raw)
            print(f'최종 출력 ({len(final)}자):\n')
            print(final)
        except Exception as e:
            print(f'오류: {e}')
    else:
        section('STEP 3 — LLM 건너뜀', ok=False)

print(f'\n{SEP}\n모든 테스트 완료\n{SEP}\n')
