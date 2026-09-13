"""对比 DeepSeek 对对象与 JSON 字符串形式的 user.content 的响应。

运行：uv run python scripts/check_deepseek_content_type.py --send
会发送两次真实模型请求，可能产生少量费用；不会打印 API Key。
"""

from __future__ import annotations

import argparse
import json

import httpx

from repopilot.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="确认发送两次可能计费的请求")
    args = parser.parse_args()
    if not args.send:
        parser.error("请添加 --send 以确认发送真实请求")

    settings = get_settings()
    if settings.model_base_url is None or settings.model_api_key is None:
        parser.error("请先在 .env 中设置 REPOPILOT_MODEL_BASE_URL 和 REPOPILOT_MODEL_API_KEY")

    api_key = settings.model_api_key.get_secret_value()
    url = f"{settings.model_base_url.rstrip('/')}/chat/completions"
    user_data = {"probe": "ping"}
    results: dict[str, int] = {}

    with httpx.Client(timeout=60) as client:
        for label, content in (
            ("object", user_data),
            ("json_string", json.dumps(user_data)),
        ):
            payload = {
                "model": settings.model_name,
                "messages": [
                    {"role": "system", "content": 'Return a JSON object: {"ok": true}.'},
                    {"role": "user", "content": content},
                ],
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 128,
                "response_format": {"type": "json_object"},
            }
            try:
                response = client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.RequestError as exc:
                print(f"{label}: 请求失败：{exc.__class__.__name__}: {exc}")
                return
            results[label] = response.status_code
            print(f"{label}: HTTP {response.status_code}")
            if response.is_error:
                print(response.text[:1000].replace(api_key, "<redacted>"))

    if results["object"] == 400 and 200 <= results["json_string"] < 300:
        print("结论：仅把 content 从对象改成字符串就通过，支持该格式错误是 400 的原因。")
    else:
        print("结论：本次对照未能单独确认原因，请查看上面的状态码和错误正文。")


if __name__ == "__main__":
    main()
