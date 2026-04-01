"""
GitHub Copilot / GitHub Models API 래퍼
OpenAI 호환 엔드포인트 사용 (openai 패키지)

GitHub Models:
  endpoint = https://models.inference.ai.azure.com
  token    = GitHub Personal Access Token

GitHub Copilot:
  endpoint = https://api.githubcopilot.com
  token    = GitHub OAuth 토큰
"""

import json

GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com"
GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL = "claude-sonnet-4-5"
BATCH_SIZE = 50          # 한 번에 LLM에 보낼 파일 수
MAX_OUTPUT_TOKENS = 4096  # 응답용 최대 토큰


class LLMClient:
    """GitHub Copilot / GitHub Models API 클라이언트"""

    def __init__(self, token: str, model: str = DEFAULT_MODEL,
                 endpoint: str = GITHUB_MODELS_ENDPOINT):
        from openai import OpenAI  # 지연 임포트 — openai 미설치 시 에러 명확화
        self.model = model
        self._client = OpenAI(base_url=endpoint, api_key=token)

    def test_connection(self) -> str:
        """연결 테스트: '2+2는 뭔가요?' 질문으로 API 동작 확인"""
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user",
                       "content": "2+2는 뭔가요? 한 줄로 간단히 답해주세요."}],
            max_tokens=100
        )
        return resp.choices[0].message.content.strip()

    def analyze_and_tag(self, filenames: list, tag_pool: list,
                        on_progress=None) -> list:
        """
        파일명 목록을 태그 풀 기반으로 LLM이 분류.
        입력:  filenames  — 파일명 문자열 리스트
               tag_pool   — 허용 태그 리스트 (LLM이 이 중에서만 선택)
               on_progress(done, total) — 진행 콜백 (선택)
        반환:  tags_list  — filenames 와 같은 순서의 태그 리스트 리스트
                           예) [["애니", "자막"], ["영화"], [], ...]
        """
        total = len(filenames)
        results: list = [[] for _ in range(total)]

        for i in range(0, total, BATCH_SIZE):
            batch = filenames[i:i + BATCH_SIZE]
            batch_tags = self._tag_batch(batch, tag_pool)
            results[i:i + len(batch)] = batch_tags
            if on_progress:
                on_progress(min(i + BATCH_SIZE, total), total)

        return results

    def _tag_batch(self, filenames: list, tag_pool: list) -> list:
        """배치 단위 분류 — 같은 순서의 태그 리스트 반환"""
        if not filenames or not tag_pool:
            return [[] for _ in filenames]

        pool_str  = ', '.join(f'"{t}"' for t in tag_pool)
        files_str = '\n'.join(f'{i + 1}. {fn}' for i, fn in enumerate(filenames))

        system_prompt = (
            "당신은 동영상 파일 분류 전문가입니다.\n"
            "파일명을 분석하여 주어진 태그 풀에서 적절한 태그를 1~3개 선택하세요.\n"
            "반드시 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요.\n"
            '응답 형식: {"1": ["태그1", "태그2"], "2": ["태그1"], ...}'
        )
        user_prompt = (
            f"태그 풀 (이 중에서만 선택): [{pool_str}]\n\n"
            f"파일 목록:\n{files_str}\n\n"
            "각 파일 번호에 맞게 태그를 선택하여 JSON으로만 응답하세요.\n"
            "판단이 어려운 경우 가장 가능성 높은 태그를 선택하세요."
        )

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=MAX_OUTPUT_TOKENS
            )
            raw = resp.choices[0].message.content.strip()

            # ```json ... ``` 블록 처리
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)

            tags_list = []
            for i in range(len(filenames)):
                raw_tags = data.get(str(i + 1), [])
                if isinstance(raw_tags, list):
                    valid = [t for t in raw_tags if t in tag_pool]
                else:
                    valid = []
                tags_list.append(valid)
            return tags_list

        except Exception:
            return [[] for _ in filenames]
