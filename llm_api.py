"""
GitHub Copilot API 래퍼
OpenAI 호환 엔드포인트 (openai 패키지 사용)

연결 방법:
  base_url = https://api.githubcopilot.com
  api_key  = GitHub Personal Access Token (PAT)
  필수 헤더 4개 포함 (없으면 401/403)
"""

import json

GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL            = "claude-sonnet-4.5"
BATCH_SIZE               = 50    # 한 번에 LLM에 보낼 파일 수
MAX_OUTPUT_TOKENS        = 4096  # 응답용 최대 토큰

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
    "파일명을 분석하여 주어진 태그 풀에서 적절한 태그를 1~3개 선택하세요.\n"
    "반드시 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요.\n"
    '응답 형식: {"1": ["태그1", "태그2"], "2": ["태그1"], ...}'
)


class LLMClient:
    """GitHub Copilot API 클라이언트 (OpenAI SDK 사용)"""

    def __init__(self, token: str,
                 model: str    = DEFAULT_MODEL,
                 endpoint: str = GITHUB_COPILOT_ENDPOINT):
        from openai import OpenAI  # 지연 임포트 — openai 미설치 시 에러 명확화
        self.model    = model
        self._client  = OpenAI(
            base_url        = endpoint,
            api_key         = token,
            default_headers = _COPILOT_HEADERS,   # ← 필수
        )

    # ── 연결 테스트 ──────────────────────────────
    def test_connection(self) -> str:
        """'2+2는 뭔가요?' 로 API 동작 확인"""
        resp = self._client.chat.completions.create(
            model    = self.model,
            messages = [{"role": "user",
                         "content": "2+2는 뭔가요? 한 줄로 간단히 답해주세요."}],
            max_tokens = 100,
        )
        return resp.choices[0].message.content.strip()

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
            resp = self._client.chat.completions.create(
                model      = self.model,
                messages   = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens = MAX_OUTPUT_TOKENS,
            )
            raw = resp.choices[0].message.content.strip()

            # ```json ... ``` 블록 제거
            if raw.startswith("```"):
                parts = raw.split("```")
                raw   = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data      = json.loads(raw)
            tags_list = []
            for i in range(len(filenames)):
                raw_tags = data.get(str(i + 1), [])
                valid    = [t for t in raw_tags if t in tag_pool] \
                           if isinstance(raw_tags, list) else []
                tags_list.append(valid)
            return tags_list

        except Exception:
            return [[] for _ in filenames]
