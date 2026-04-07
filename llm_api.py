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
    def _chat(self, messages: list, max_tokens: int = 100) -> str:
        content, _, _ = self._chat_tracked(messages, max_tokens)
        return content

    def _chat_tracked(self, messages: list, max_tokens: int = 100) -> tuple:
        """(content, prompt_tokens, completion_tokens) 반환"""
        url     = f"{self._endpoint}/chat/completions"
        payload = {
            "model":      self.model,
            "messages":   messages,
            "max_tokens": max_tokens,
        }
        # 출력 토큰이 클수록 더 긴 타임아웃 필요
        # 보수적으로 30 tok/s 기준, 최소 120초
        http_timeout = max(120, max_tokens // 30)
        with httpx.Client(timeout=http_timeout) as client:
            resp = client.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            body    = resp.json()
            content = body["choices"][0]["message"]["content"].strip()
            usage   = body.get("usage", {})
            return (content,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0))

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
                max_tokens=400 + len(eligible) * 60,
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
