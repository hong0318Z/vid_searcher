"""
로컬 LLM 서버(oMLX 등) 500 에러 원인 진단용 독립 테스트 스크립트.
vidsort 앱 없이 단독으로 실행해서, 어떤 조건에서 500이 나는지 좁혀본다.

사용법:
  python3 test_local_llm.py --endpoint http://192.168.0.116:8000/v1 --model Qwen3.6-35B-A3B-4bit

옵션 없이 실행하면 기본값(현재 대시보드에 보이는 서버 주소/모델)으로 시도한다.
"""

import argparse
import json
import sys

import httpx

DEFAULT_ENDPOINT = "http://192.168.0.116:8000/v1"
DEFAULT_MODEL    = "Qwen3.6-35B-A3B-4bit"


def _print_result(name, resp):
    print(f"\n--- {name} ---")
    print(f"status: {resp.status_code}")
    body = resp.text
    print(f"body  : {body[:1000]}")


def _post(client, url, headers, payload, name):
    try:
        resp = client.post(url, json=payload, headers=headers)
        _print_result(name, resp)
        return resp
    except Exception as e:
        print(f"\n--- {name} ---")
        print(f"요청 자체 실패: {type(e).__name__}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default="")
    args = ap.parse_args()

    endpoint = args.endpoint.rstrip("/")
    headers_with_auth = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.api_key or 'local'}",
    }

    timeout = httpx.Timeout(connect=10, read=120, write=10, pool=5)
    with httpx.Client(timeout=timeout) as client:

        # 1) 모델 목록 — 서버가 보고하는 실제 모델 id 확인 (이름이 한 글자라도 다르면 500 나는 서버가 많음)
        print("=== 1) GET /models ===")
        try:
            resp = client.get(f"{endpoint}/models", headers=headers_with_auth)
            _print_result("models", resp)
            if resp.status_code == 200:
                ids = [m.get("id") for m in resp.json().get("data", [])]
                print(f"등록된 모델 id 목록: {ids}")
                if args.model not in ids:
                    print(f"!!! 주의: --model 로 지정한 '{args.model}' 이 목록에 없음 — "
                          f"모델 이름 불일치가 500의 흔한 원인입니다.")
        except Exception as e:
            print(f"models 조회 실패: {e}")

        # 2) 아주 작은 chat 요청 (max_tokens 최소, 메시지 1줄) — 기본 동작 자체가 되는지
        small_payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": "안녕"}],
            "max_tokens": 16,
            "stream": False,
        }
        _post(client, f"{endpoint}/chat/completions", headers_with_auth, small_payload,
              "2) 최소 요청 (max_tokens=16)")

        # 3) system + user 2개 메시지, max_tokens 중간 (실제 classify_videos와 형태는 비슷하되 더 짧게)
        mid_payload = {
            "model": args.model,
            "messages": [
                {"role": "system", "content": "당신은 분류 전문가입니다. JSON으로만 답하세요."},
                {"role": "user", "content": '파일: 1. test.mp4\n분류해주세요.'},
            ],
            "max_tokens": 256,
            "stream": False,
        }
        _post(client, f"{endpoint}/chat/completions", headers_with_auth, mid_payload,
              "3) system+user, max_tokens=256")

        # 4) 실제 앱이 보내는 것과 동일한 큰 max_tokens (32768) — 여기서만 500이면 컨텍스트/메모리 문제일 가능성
        big_payload = dict(mid_payload)
        big_payload["max_tokens"] = 32768
        _post(client, f"{endpoint}/chat/completions", headers_with_auth, big_payload,
              "4) 동일 요청, max_tokens=32768 (앱과 동일)")

        # 5) Authorization 헤더 없이 — 헤더 누락이 원인인지 재확인
        headers_no_auth = {"Content-Type": "application/json"}
        _post(client, f"{endpoint}/chat/completions", headers_no_auth, small_payload,
              "5) Authorization 헤더 없이, 최소 요청")

    print("\n=== 결과 해석 가이드 ===")
    print("- 2)는 되고 4)만 500 -> max_tokens 32768이 너무 커서 서버가 죽는 것(메모리/컨텍스트 한도). "
          "local_llm.py의 MAX_OUTPUT_TOKENS를 다시 낮춰야 함.")
    print("- 2)~4) 전부 500, 5)만 다른 결과 -> 인증 헤더 문제.")
    print("- 1)에서 모델 id 불일치 경고가 뜸 -> --model 값을 정확한 id로 맞춰야 함.")
    print("- 전부 동일하게 500 -> 서버(oMLX) 자체 문제 — 서버 콘솔/로그를 직접 확인 필요.")


if __name__ == "__main__":
    main()
