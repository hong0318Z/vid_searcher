"""
GitHub Copilot API 래퍼
httpx 직접 호출 (openai 패키지 불필요 — EXE 크기 절감)

연결 방법:
  endpoint = https://api.githubcopilot.com
  api_key  = GitHub Personal Access Token (PAT)
  필수 헤더 4개 포함 (없으면 401/403)
"""

import json
import httpx

GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL            = "claude-sonnet-4.5"
BATCH_SIZE               = 50      # 한 번에 LLM에 보낼 파일 수
MAX_OUTPUT_TOKENS        = 64000   # claude-sonnet-4.5 최대 출력 토큰

# GitHub Copilot API 필수 헤더 — 없으면 401/403
_COPILOT_HEADERS = {
    "Editor-Version":         "vscode/1.95.0",
    "Editor-Plugin-Version":  "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "Openai-Organization":    "github-copilot",
}

# 기본 시스템 프롬프트 (설정창에서 사용자가 덮어쓸 수 있음)
DEFAULT_SYSTEM_PROMPT = (
    "당신은 동영상 파일 분류 전문가입니다.\n"
    "파일명을 분석하여 적절한 태그를 1~3개 선택하세요.\n"
    "기존 태그 풀에 가장 유사한 태그를 우선 사용하고,\n"
    "아예 적합한 태그가 없으면 'NEW:새태그명' 형식으로 새 태그를 만드세요.\n"
    "반드시 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요.\n"
    '응답 형식: {"1": ["태그1", "태그2"], "2": ["NEW:새장르"], ...}'
)


class LLMClient:
    """GitHub Copilot API 클라이언트 (httpx 직접 호출)"""

    def __init__(self, token: str,
                 model: str    = DEFAULT_MODEL,
                 endpoint: str = GITHUB_COPILOT_ENDPOINT):
        self.model     = model
        self._endpoint = endpoint.rstrip('/')
        self._headers  = {
            **_COPILOT_HEADERS,
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

    # ── 내부 호출 ────────────────────────────────
    def _chat(self, messages: list, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
        content, _, _ = self._chat_tracked(messages, max_tokens)
        return content

    def _chat_tracked(self, messages: list, max_tokens: int = MAX_OUTPUT_TOKENS,
                      on_chunk: callable = None) -> tuple:
        """(content, prompt_tokens, completion_tokens) 반환.
        스트리밍으로 수신 — 토큰이 오는 즉시 누적, 타임아웃은 청크 간격 기준."""
        url     = f"{self._endpoint}/chat/completions"
        payload = {
            "model":      self.model,
            "messages":   messages,
            "max_tokens": max_tokens,
            "stream":     True,
        }
        # 스트리밍: 청크 사이 60초 무응답이면 끊어짐 (전체 응답 대기 X)
        with httpx.Client(timeout=httpx.Timeout(connect=30, read=60,
                                                write=30, pool=10)) as client:
            chunks   = []
            tok_in   = 0
            tok_out  = 0
            with client.stream('POST', url, json=payload,
                               headers=self._headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    # 사용량 (마지막 청크에 포함되는 경우)
                    if 'usage' in obj:
                        u = obj['usage']
                        tok_in  = u.get('prompt_tokens', tok_in)
                        tok_out = u.get('completion_tokens', tok_out)
                    delta = (obj.get('choices') or [{}])[0].get('delta', {})
                    piece = delta.get('content') or ''
                    if piece:
                        chunks.append(piece)
                        tok_out += 1   # 청크별 카운트 (usage 없을 때 추정)
                        if on_chunk:
                            on_chunk(piece)
            content = ''.join(chunks).strip()
            return content, tok_in, tok_out

    # ── 연결 테스트 ──────────────────────────────
    def test_connection(self) -> str:
        """'2+2는 뭔가요?' 로 API 동작 확인"""
        return self._chat(
            [{"role": "user",
              "content": "2+2는 뭔가요? 한 줄로 간단히 답해주세요."}]
        )

    # ── 배치 자동 태그 ───────────────────────────
    def analyze_and_tag(self, filenames: list, tag_pool: list,
                        on_progress=None,
                        custom_prompt: str = "") -> list:
        """
        파일명 목록을 태그 풀 기반으로 LLM이 분류.

        filenames      — 파일명 문자열 리스트
        tag_pool       — 허용 태그 리스트 (LLM이 이 중에서만 선택)
        on_progress    — on_progress(done, total) 진행 콜백 (선택)
        custom_prompt  — 사용자 정의 시스템 프롬프트 (비어 있으면 기본값 사용)

        반환: filenames 와 같은 순서의 태그 리스트
              예) [["애니", "자막"], ["영화"], [], ...]
        """
        system_prompt = custom_prompt.strip() if custom_prompt.strip() \
                        else DEFAULT_SYSTEM_PROMPT

        total   = len(filenames)
        results = [[] for _ in range(total)]

        for i in range(0, total, BATCH_SIZE):
            batch      = filenames[i:i + BATCH_SIZE]
            batch_tags = self._tag_batch(batch, tag_pool, system_prompt)
            results[i:i + len(batch)] = batch_tags
            if on_progress:
                on_progress(min(i + BATCH_SIZE, total), total)

        return results

    def analyze_and_name(self, filenames: list,
                         on_progress=None) -> list:
        """
        파일명 → 한글 제목(alias) + 설명(description) 생성.
        5글자 미만 stem은 빈 결과 반환.

        반환: [{"alias": "...", "description": "..."}, ...]  (filenames와 동일 순서)
        """
        total   = len(filenames)
        results = [{"alias": "", "description": ""} for _ in range(total)]

        for i in range(0, total, BATCH_SIZE):
            batch = filenames[i:i + BATCH_SIZE]
            batch_res = self._name_batch(batch)
            results[i:i + len(batch)] = batch_res
            if on_progress:
                on_progress(min(i + BATCH_SIZE, total), total)

        return results

    def _name_batch(self, filenames: list) -> list:
        """파일명 배치 → 한글 이름 + 설명"""
        from pathlib import Path as _Path
        eligible = [(idx, fn) for idx, fn in enumerate(filenames)
                    if len(_Path(fn).stem) >= 5]
        results = [{"alias": "", "description": ""} for _ in filenames]
        if not eligible:
            return results

        lines = '\n'.join(f"{j+1}. {fn}" for j, (_, fn) in enumerate(eligible))
        prompt = (
            "동영상 파일명을 분석하여 한국어 제목과 간단한 설명을 생성하세요.\n"
            "제목은 파일명 뜻을 살린 자연스러운 한국어로,\n"
            "설명은 내용을 추측한 2문장 이내로 작성하세요.\n"
            "반드시 JSON만 출력: "
            '{"1":{"alias":"한글제목","description":"설명"},"2":{...},...}\n\n'
            f"파일 목록:\n{lines}"
        )
        try:
            raw = self._chat(
                [{"role": "user", "content": prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = json.loads(raw.strip())
            for j, (orig_idx, _) in enumerate(eligible):
                entry = data.get(str(j + 1), {})
                results[orig_idx] = {
                    "alias":       entry.get("alias", "").strip(),
                    "description": entry.get("description", "").strip(),
                }
        except Exception:
            pass
        return results

    # ── AI 영상 추천 (2회 호출) ──────────────────
    def recommend_query(self, user_query: str, tag_pool: list,
                        on_chunk: callable = None) -> dict:
        """[1차 호출] 자연어 쿼리 → 검색 태그 + 키워드 JSON 추출.
        반환: {"tags": [...], "keywords": [...], "intent": "..."}"""
        tag_list_str = ', '.join(f'"{t}"' for t in tag_pool[:300])

        system = (
            "당신은 영상 라이브러리 검색 전문가입니다.\n"
            "사용자의 자연어 검색어를 분석하여 DB 검색에 사용할 태그와 키워드를 추출하세요.\n"
            "태그는 반드시 주어진 목록에서만 선택하세요. 키워드는 파일명/폴더명에서 찾을 자유 검색어입니다.\n"
            "반드시 JSON만 출력하세요. 그 외 텍스트는 절대 출력하지 마세요."
        )
        user = (
            f'검색어: "{user_query}"\n\n'
            f'사용 가능한 태그 목록:\n[{tag_list_str}]\n\n'
            '응답 형식 (JSON만):\n'
            '{\n'
            '  "tags": ["태그1", "태그2"],\n'
            '  "keywords": ["파일명검색어1", "파일명검색어2"],\n'
            '  "intent": "검색 의도 한 줄 요약"\n'
            '}'
        )
        try:
            content, _, _ = self._chat_tracked(
                [{"role": "system", "content": system},
                 {"role": "user",   "content": user}],
                max_tokens=MAX_OUTPUT_TOKENS,
                on_chunk=on_chunk,
            )
            if content.startswith('```'):
                content = content.split('```')[1].lstrip('json').strip()
            return json.loads(content.strip())
        except Exception:
            # 파싱 실패 시 쿼리 자체를 키워드로
            return {"tags": [], "keywords": [user_query.strip()], "intent": user_query}

    def recommend_explain(self, user_query: str, videos: list,
                          on_chunk: callable = None) -> str:
        """[2차 호출] 찾은 영상 목록을 가게 점원 스타일로 설명.
        videos: [{"name": ..., "alias": ..., "tags": [...], "duration": ...}, ...]"""
        lines = []
        for i, v in enumerate(videos[:50], 1):
            title = v.get('alias') or v.get('name', '')
            tags  = ', '.join((v.get('tags') or [])[:5])
            dur   = v.get('duration_str', '')
            line  = f"{i}. 『{title}』"
            if tags:
                line += f"  [태그: {tags}]"
            if dur:
                line += f"  ({dur})"
            lines.append(line)
        vid_str = '\n'.join(lines)

        system = (
            "당신은 동네 작은 영상 가게의 친절하고 유쾌한 점원입니다.\n"
            "손님이 원하는 영상을 찾아드리는 게 낙이에요.\n"
            "자연스럽고 친근한 말투로, 마치 실제 가게에서 이야기하듯 설명해주세요.\n"
            "찾아드린 영상 중 특히 잘 맞을 것 같은 3~5개를 콕 집어서 "
            "왜 추천하는지 이유도 함께 말해주세요.\n"
            "전체 목록 기준으로 왜 이것들을 골랐는지도 간략히 설명해주세요.\n"
            "너무 길지 않게, 자연스럽게 대화하듯 써주세요."
        )
        user = (
            f'손님이 찾으시는 영상: "{user_query}"\n\n'
            f'제가 찾아드린 영상 ({len(videos[:50])}개):\n{vid_str}\n\n'
            '가게 점원으로서 이 영상들을 소개해주세요.'
        )
        try:
            content, _, _ = self._chat_tracked(
                [{"role": "system", "content": system},
                 {"role": "user",   "content": user}],
                max_tokens=MAX_OUTPUT_TOKENS,
                on_chunk=on_chunk,
            )
            return content
        except Exception as e:
            return f"(설명 생성 실패: {e})"

    def analyze_actor_names(self, names: list) -> dict:
        """배우 이름 목록 분석 → JAV/서양 분류 + 영문 슬러그 추출.
        반환: {
          "배우명": {
            "type": "jav"|"western"|"unknown",
            "javdb_slug": "rei-saegusa",     # javdatabase.com 용 (하이픈, 소문자)
            "babepedia_slug": "Rei_Saegusa", # babepedia.com 용 (언더바, 대소문자)
            "variants": ["slug1", "slug2"]   # 추가 시도 슬러그
          }
        }"""
        if not names:
            return {}

        _PROMPT_HEADER = (
            '아래 AV 배우 이름 목록을 분석해주세요.\n'
            '각 배우에 대해:\n'
            '1. JAV(일본 성인영상) 배우인지, 서양(Western) 배우인지 판단\n'
            '2. javdatabase.com URL 슬러그 추출 (소문자, 하이픈 구분, 예: rei-saegusa)\n'
            '3. babepedia.com URL 슬러그 추출 (언더바 구분, 대소문자, 예: Rei_Saegusa)\n'
            '4. 오타/별명이 있을 경우 추가 슬러그 variants에 포함\n\n'
            '반드시 JSON만 출력:\n'
            '{\n'
            '  "배우명": {\n'
            '    "type": "jav"|"western"|"unknown",\n'
            '    "javdb_slug": "영문-하이픈",\n'
            '    "babepedia_slug": "English_Underscore",\n'
            '    "variants": ["alt-slug1", "Alt_Slug2"]\n'
            '  }, ...\n'
            '}\n\n'
            '배우 목록:\n'
        )
        # 입력 토큰 추정 (4자 ≈ 1토큰), MAX_OUTPUT_TOKENS의 60% 초과 시 분할
        _MAX_INPUT_CHARS = int(MAX_OUTPUT_TOKENS * 0.6 * 4)
        lines = '\n'.join(f'{i+1}. {n}' for i, n in enumerate(names))
        if len(_PROMPT_HEADER) + len(lines) > _MAX_INPUT_CHARS:
            mid = len(names) // 2
            result = {}
            result.update(self.analyze_actor_names(names[:mid]))
            result.update(self.analyze_actor_names(names[mid:]))
            return result

        prompt = _PROMPT_HEADER + lines
        try:
            raw = self._chat(
                [{'role': 'system',
                  'content': '당신은 성인 영상 배우 데이터베이스 전문가입니다. JSON만 출력하세요.'},
                 {'role': 'user', 'content': prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            if raw.startswith('```'):
                raw = raw.split('```')[1].lstrip('json').strip()
            brace = raw.find('{')
            if brace > 0: raw = raw[brace:]
            return json.loads(raw.strip())
        except Exception:
            return {}

    def classify_tags(self, tag_list: list) -> dict:
        """태그 목록 → {"태그명": "행위"|"인물"|"레이블"|"기타"} 분류.
        판단 불가한 경우 "기타"로 반환."""
        if not tag_list:
            return {}
        lines = '\n'.join(f'{i+1}. {t}' for i, t in enumerate(tag_list))
        prompt = (
            '아래 영상 태그 목록을 분류해주세요.\n'
            '각 태그가 어떤 유형인지 판단하세요:\n'
            '- "인물": 배우/출연자 이름 (예: 사쿠라 미코, 아오이 쇼코, 山田花子)\n'
            '- "행위": 성행위/장르/플레이 종류 (예: 야외노출, 구강, 긴박)\n'
            '- "레이블": 제작사/스튜디오/브랜드 이름 (예: SOD, エスワン, Faleno)\n'
            '- "기타": 위 분류에 해당하지 않는 것 (설정, 분위기, 화질 등)\n\n'
            '반드시 JSON만 출력하세요. 다른 설명 없이 JSON만:\n'
            '{"태그명": "인물"|"행위"|"레이블"|"기타", ...}\n\n'
            f'태그 목록:\n{lines}'
        )
        try:
            raw = self._chat(
                [{'role': 'system',
                  'content': '당신은 성인 영상 태그 분류 전문가입니다. JSON만 출력하세요.'},
                 {'role': 'user', 'content': prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            if raw.startswith('```'):
                raw = raw.split('```')[1].lstrip('json').strip()
            brace = raw.find('{')
            if brace > 0:
                raw = raw[brace:]
            data = json.loads(raw.strip())
            valid = {'인물', '행위', '레이블', '기타'}
            return {k: v for k, v in data.items() if v in valid}
        except Exception:
            return {}

    def generate_actor_info(self, actor_name: str,
                            raw_scraped: str = '') -> str:
        """배우 이름 + 스크래핑 원본 → 한국어 배우 프로필 텍스트."""
        prompt = (
            f'다음은 {actor_name}에 대해 여러 소스에서 수집한 정보입니다.\n'
            '이 정보들을 적절히 섞어서 자연스러운 한국어 배우 소개글을 작성하세요.\n'
            '형식 (모르는 항목은 생략): 이름 / 생년월일 / 신장 / 데뷔 / 국적 / 활동 / 특이사항 순으로.\n'
            '없는 정보는 생략. 최대 2000자.\n\n'
            f'수집된 원본 정보:\n{raw_scraped or "(없음)"}'
        )
        try:
            return self._chat(
                [{'role': 'system',
                  'content': '당신은 성인 영상 배우 정보 정리 전문가입니다.'},
                 {'role': 'user', 'content': prompt}],
                max_tokens=2048,
            )
        except Exception as e:
            return f'(정보 생성 실패: {e})'

    def generate_action_desc(self, action_name: str,
                             context_tags: str = '') -> str:
        """행위 태그 → 한국어 설명 텍스트."""
        ctx = f'\n관련 태그 컨텍스트: {context_tags}' if context_tags else ''
        prompt = (
            f'성인 영상 장르/행위 태그: "{action_name}"{ctx}\n\n'
            '이 태그가 어떤 행위/장르를 의미하는지 한국어로 간결하게 설명해주세요.\n'
            '100자 이내, 설명체로 작성하세요.'
        )
        try:
            return self._chat(
                [{'role': 'user', 'content': prompt}],
                max_tokens=512,
            )
        except Exception as e:
            return f'(설명 생성 실패: {e})'

    def _tag_batch(self, filenames: list, tag_pool: list,
                   system_prompt: str) -> list:
        """배치 단위 분류 — 같은 순서의 태그 리스트 반환"""
        if not filenames or not tag_pool:
            return [[] for _ in filenames]

        pool_str  = ", ".join(f'"{t}"' for t in tag_pool)
        files_str = "\n".join(f"{i+1}. {fn}" for i, fn in enumerate(filenames))

        user_prompt = (
            f"태그 풀 (이 중에서만 선택): [{pool_str}]\n\n"
            f"파일 목록:\n{files_str}\n\n"
            "각 파일 번호에 맞게 태그를 선택하여 JSON으로만 응답하세요.\n"
            "판단이 어려운 경우 가장 가능성 높은 태그를 선택하세요."
        )

        try:
            raw = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=MAX_OUTPUT_TOKENS,
            )

            # ```json ... ``` 블록 제거
            if raw.startswith("```"):
                parts = raw.split("```")
                raw   = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data      = json.loads(raw)
            tags_list = []
            pool_set  = set(tag_pool)
            for i in range(len(filenames)):
                raw_tags = data.get(str(i + 1), [])
                valid = []
                if isinstance(raw_tags, list):
                    for t in raw_tags:
                        if isinstance(t, str):
                            if t.startswith('NEW:'):
                                new_t = t[4:].strip()
                                if new_t:
                                    valid.append(new_t)   # 새 태그 허용
                            elif t in pool_set:
                                valid.append(t)
                tags_list.append(valid)
            return tags_list

        except Exception:
            return [[] for _ in filenames]
