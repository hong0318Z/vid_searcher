"""
로컬 LLM 클라이언트 (oMLX 등 OpenAI-compatible 서버용)
httpx 직접 호출 — chat/completions, embeddings, models 엔드포인트 사용.

연결 방법:
  endpoint = http://127.0.0.1:8000/v1 (oMLX 등)
  api_key  = 없으면 빈 문자열
"""

import json
import httpx

DEFAULT_ENDPOINT  = "http://127.0.0.1:8000/v1"
MAX_OUTPUT_TOKENS  = 32768
CLASSIFY_BATCH_MAX = 25   # 폴더 클러스터당 LLM에 보낼 최대 영상 수

DEFAULT_CLASSIFY_PROMPT = (
    "당신은 동영상 파일 분류 전문가입니다.\n"
    "각 파일은 '최상위폴더 기준 상대경로' 형태로 주어집니다.\n"
    "예: '동물/BBC/아프리카/사자의왕국.mp4' → 중간 폴더명(동물, BBC, 아프리카)이\n"
    "분류/태그를 암시하는 강한 단서입니다. 폴더 구조를 최대한 활용하세요.\n"
    "이를 바탕으로 각 영상에 어울리는 대분류(category, 1개)와\n"
    "소분류 태그(tag, 1~3개)를 제안하세요.\n"
    "반드시 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요.\n"
    '응답 형식: {"1": {"category": "다큐", "tags": ["동물", "아프리카"]}, "2": {...}, ...}'
)

DEFAULT_NORMALIZE_PROMPT = (
    "당신은 태그 정규화 전문가입니다.\n"
    "새로 제안된 태그(raw_tag)들과 기존 태그 후보 목록을 비교하여,\n"
    "의미가 같거나 매우 유사하면 기존 후보 중 하나를 그대로 사용하고,\n"
    "정말 새로운 의미라면 raw_tag 그대로 새 태그로 채택하세요.\n"
    "반드시 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요.\n"
    '응답 형식: {"raw_tag1": "최종태그", "raw_tag2": "최종태그", ...}'
)


class LocalLLMClient:
    """oMLX 등 OpenAI-compatible 로컬 LLM 서버 클라이언트"""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, api_key: str = "",
                 chat_model: str = "", embed_model: str = ""):
        self._endpoint   = endpoint.rstrip('/')
        self.chat_model  = chat_model
        self.embed_model = embed_model
        self._headers = {"Content-Type": "application/json"}
        # API 키가 비어 있어도 더미 Bearer 토큰을 보냄 — 일부 로컬 서버(oMLX 등)는
        # Authorization 헤더 자체가 없으면 401이 아니라 500을 내는 경우가 있음
        self._headers["Authorization"] = f"Bearer {api_key or 'local'}"

    # ── 모델 목록 ────────────────────────────────
    def list_models(self) -> list:
        """GET {endpoint}/models → id 리스트"""
        url = f"{self._endpoint}/models"
        with httpx.Client(timeout=httpx.Timeout(connect=10, read=15,
                                                  write=10, pool=5)) as client:
            resp = client.get(url, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
            return [m.get('id', '') for m in data.get('data', []) if m.get('id')]

    # ── 임베딩 ───────────────────────────────────
    def embed_texts(self, texts: list) -> list:
        """POST {endpoint}/embeddings → list[list[float]] (texts와 같은 순서)"""
        if not texts:
            return []
        url     = f"{self._endpoint}/embeddings"
        payload = {"model": self.embed_model, "input": texts}
        with httpx.Client(timeout=httpx.Timeout(connect=10, read=60,
                                                  write=30, pool=10)) as client:
            resp = client.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
            items = sorted(data.get('data', []), key=lambda d: d.get('index', 0))
            return [it.get('embedding', []) for it in items]

    # ── 내부 chat 호출 ───────────────────────────
    def _chat(self, messages: list, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
        url     = f"{self._endpoint}/chat/completions"
        payload = {
            "model":      self.chat_model,
            "messages":   messages,
            "max_tokens": max_tokens,
            "stream":     False,
        }
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=300,
                                                  write=30, pool=10)) as client:
            resp = client.post(url, json=payload, headers=self._headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"{e} — 응답 본문: {resp.text[:500]}") from e
            data = resp.json()
            return (data.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        return raw.strip()

    @staticmethod
    def _parse_indexed_json(raw: str, n: int) -> list:
        """{"1":{...},"2":{...}} 또는 [{...},{...}] 형태 모두 허용해 길이 n 리스트로 정규화."""
        data = json.loads(raw)
        if isinstance(data, list):
            return [data[i] if i < len(data) and isinstance(data[i], dict) else {}
                    for i in range(n)]
        if isinstance(data, dict):
            out = []
            for i in range(n):
                entry = data.get(str(i + 1))
                if entry is None:
                    entry = data.get(i + 1)   # 정수 키로 응답하는 모델 대응
                out.append(entry if isinstance(entry, dict) else {})
            return out
        raise ValueError(f"unexpected JSON shape: {type(data)}")

    # ── 영상 분류 ────────────────────────────────
    def classify_videos(self, filenames: list, folder_ctx: str = "",
                        rejection_notes: dict = None,
                        custom_prompt: str = "",
                        on_debug: callable = None) -> list:
        """
        filenames        — 파일명(혹은 상대경로) 리스트 (최대 CLASSIFY_BATCH_MAX 권장)
        folder_ctx        — 폴더 경로/맥락 설명 문자열
        rejection_notes   — {filename: [{"suggested":..., "comment":...}, ...]} (과거 거부 이력)
        custom_prompt     — 사용자 지정 지침 (시스템 프롬프트 뒤에 추가됨)
        on_debug          — on_debug(stage, raw_text, error) 콜백 — 매 호출 후 raw 응답/에러 전달

        반환: filenames와 같은 순서의 [{"category": "...", "tags": [...]}, ...]
        """
        rejection_notes = rejection_notes or {}
        system = DEFAULT_CLASSIFY_PROMPT
        if custom_prompt and custom_prompt.strip():
            system = system + "\n\n[사용자 지침]\n" + custom_prompt.strip()

        lines = []
        for i, fn in enumerate(filenames):
            notes = rejection_notes.get(fn) or []
            line = f"{i+1}. {fn}"
            if notes:
                hist = "; ".join(
                    f"이전 제안 {n.get('suggested')} 거부됨"
                    + (f" (사유: {n.get('comment')})" if n.get('comment') else "")
                    for n in notes
                )
                line += f"  [과거 거부 이력: {hist} → 이를 고려해 다른 제안을 하세요]"
            lines.append(line)

        user = (
            f"폴더 맥락: {folder_ctx}\n\n"
            f"파일 목록:\n" + "\n".join(lines)
        )

        results = [{"category": "", "tags": []} for _ in filenames]
        raw = ""
        try:
            raw = self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            entries = self._parse_indexed_json(self._strip_json_fence(raw), len(filenames))
            for i, entry in enumerate(entries):
                results[i] = {
                    "category": entry.get("category", "") or "",
                    "tags": [t for t in (entry.get("tags") or []) if t],
                }
            if on_debug:
                on_debug('classify_videos', raw, None)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as e:
            if on_debug:
                on_debug('classify_videos', raw, e)
        return results

    # ── 태그 정규화 (2단계: 임베딩 후보 → LLM 최종 판단) ──
    def normalize_tags(self, raw_tags: list, candidates: dict,
                       on_debug: callable = None) -> dict:
        """
        raw_tags    — 새로 제안된 태그 문자열 리스트 (중복 제거된 set 권장)
        candidates  — {raw_tag: [기존 태그 후보, ...]} (임베딩 유사도로 미리 추린 목록)
        on_debug    — on_debug(stage, raw_text, error) 콜백

        반환: {raw_tag: 최종_태그}  (후보 중 하나거나 raw_tag 그대로)
        """
        if not raw_tags:
            return {}

        # 후보가 전혀 없는 raw_tag는 LLM 호출 없이 그대로 신규 태그로 채택
        needs_llm = {rt: candidates.get(rt) or [] for rt in raw_tags if candidates.get(rt)}
        result = {rt: rt for rt in raw_tags}
        if not needs_llm:
            return result

        lines = '\n'.join(
            f'- "{rt}" → 후보: {cands}' for rt, cands in needs_llm.items()
        )
        user = (
            "다음 신규 제안 태그와 기존 후보 목록을 비교해 최종 태그를 결정하세요.\n"
            f"{lines}"
        )
        raw = ""
        try:
            raw = self._chat([
                {"role": "system", "content": DEFAULT_NORMALIZE_PROMPT},
                {"role": "user", "content": user},
            ])
            data = json.loads(self._strip_json_fence(raw))
            for rt in needs_llm:
                final = data.get(rt)
                if final and isinstance(final, str):
                    result[rt] = final
            if on_debug:
                on_debug('normalize_tags', raw, None)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as e:
            if on_debug:
                on_debug('normalize_tags', raw, e)
        return result
