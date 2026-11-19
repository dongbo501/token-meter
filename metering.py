"""Budget calculations, provider settings and the synthetic workload."""

import json
import random
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

PICO = 10**12
DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "api_key": "",
    "api_style": "codex",
    "reasoning_effort": "default",
    "bark_url": "",
    "mode": "usd",
    "usd_target": 600,
    "usd_limit": 601,
    "token_target": 1000000,
    "token_limit": 1001000,
    "concurrency": 2,
    "max_output": 2048,
    "output_field": "max_tokens",
    "input_price": None,
    "cached_price": None,
    "output_price": None,
}


def number(value, label, low, high, places=0):
    try:
        if isinstance(value, bool):
            raise TypeError
        n = Decimal(str(value))
        if (
            not n.is_finite()
            or not Decimal(str(low)) <= n <= Decimal(str(high))
            or n != n.quantize(Decimal(1).scaleb(-places))
        ):
            raise ValueError
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError(
            f"{label}须在 {low} 至 {high} 之间，最多 {places} 位小数"
        ) from None
    return float(n) if places else int(n)


def endpoint(base, style="chat"):
    url = urlsplit(base.strip())
    if (
        url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("接口地址须为完整的 HTTP(S) 地址，不含凭据、查询参数或片段")
    if any(c.isspace() or ord(c) < 32 for c in base) or len(base) > 2048:
        raise ValueError("接口地址格式无效")
    _ = url.port  # Validate the port before saving the configuration.
    path = url.path.rstrip("/") or "/v1"
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path += "/chat/completions" if style == "chat" else "/responses"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def configure(old, raw):
    c = dict(old)
    for k in (
        "base_url",
        "model",
        "api_key",
        "mode",
        "output_field",
        "api_style",
        "reasoning_effort",
    ):
        value = raw.get(k, "")
        if not isinstance(value, str):
            raise TypeError("设置值须为文本")
        if value.strip():
            c[k] = value.strip()
    if raw.get("remove_bark") is True:
        c["bark_url"] = ""
    elif raw.get("bark_url"):
        address = raw["bark_url"]
        if (
            not isinstance(address, str)
            or len(address) > 2048
            or any(ch.isspace() for ch in address)
        ):
            raise ValueError("Bark 推送地址无效")
        url = urlsplit(address)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or not url.path.strip("/")
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError(
                "Bark 地址须包含服务器和设备 Key，如 https://api.day.app/设备Key"
            )
        _ = url.port
        c["bark_url"] = address.rstrip("/")
    new_url = endpoint(c["base_url"])
    if new_url != endpoint(old["base_url"]) and not raw.get("api_key", "").strip():
        raise ValueError("更改接口地址时，请重新填写该接口的 API Key")
    if not c["api_key"] or not c["model"]:
        raise ValueError("请填写模型名称和 API Key")
    if (
        len(c["api_key"]) > 4096
        or not c["api_key"].isascii()
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in c["api_key"])
        or len(c["model"]) > 256
    ):
        raise ValueError("API Key 或模型名称格式无效")
    if c["mode"] not in {"usd", "tokens"} or c["output_field"] not in {
        "max_tokens",
        "max_completion_tokens",
    }:
        raise ValueError("预算模式或输出参数无效")
    if c.get("api_style") not in {"chat", "responses", "codex"}:
        raise ValueError("执行方式无效")
    if c.get("reasoning_effort") not in {
        "default",
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("推理强度无效，请选择模型默认或列表中的强度")
    limits = {
        "usd_target": ("美元目标", 0.01, 100000, 2),
        "usd_limit": ("美元上限", 0.01, 100000, 2),
        "token_target": ("Token 目标", 1, 10**12, 0),
        "token_limit": ("Token 上限", 1, 10**12, 0),
        "concurrency": ("并发数", 1, 32, 0),
        "max_output": ("单次输出上限", 1, 32768, 0),
        "input_price": ("输入单价", 0, 10000, 6),
        "cached_price": ("缓存单价", 0, 10000, 6),
        "output_price": ("输出单价", 0, 10000, 6),
    }
    for k, rules in limits.items():
        c[k] = number(raw.get(k, c[k]), *rules)
    if c["api_style"] != "chat" and c["max_output"] < 16:
        raise ValueError("Responses / Codex 的单次输出上限至少为 16")
    if c["usd_limit"] < c["usd_target"] or c["cached_price"] > c["input_price"]:
        raise ValueError("美元上限须至少等于目标，缓存单价须低于或等于普通输入单价")
    if c["token_limit"] < c["token_target"]:
        raise ValueError("Token 上限须至少等于目标")
    if c["mode"] == "tokens" and max(prices(c)) * c["token_limit"] > 8 * 10**18:
        raise ValueError("按当前单价计算的预算超过可记录金额范围")
    if c["mode"] == "usd" and c["input_price"] == c["output_price"] == 0:
        raise ValueError("美元模式需要有效的计费单价")
    return c


def prices(c):
    return tuple(
        int(Decimal(str(c[k])) * 10**6)
        for k in ("input_price", "cached_price", "output_price")
    )


def cost(prompt, cached, output, rates):
    inp, hit, out = rates
    return (prompt - (cached or 0)) * inp + (cached or 0) * hit + output * out


def dollars(value):
    return int(Decimal(str(value)) * PICO)


def week_of(ts, zone, start_day):
    day = datetime.fromtimestamp(ts, zone).date()
    return day - timedelta(days=(day.weekday() - start_day) % 7)


def workload(style="chat"):
    rng = random.SystemRandom()
    series = [rng.randrange(15, 2000) for _ in range(16)]
    domain = rng.choice(
        [
            "book rentals",
            "museum visits",
            "bike trips",
            "bakery orders",
            "bus boardings",
            "cafe sales",
        ]
    )
    question = rng.choice(
        [
            "compare weekdays with weekends",
            "assess trend stability",
            "identify unusual days",
            "test two plausible explanations",
            "compare rolling averages",
        ]
    )
    # Unique input begins at the first token, before any shared instructions.
    if style == "codex":
        return (
            f"{secrets.token_hex(16)} | coding exercise | sample={json.dumps(series)}\n"
            f"Implement a pure Python function to {question} for daily {domain}. "
            "Provide the implementation, a worked example using the sample, edge-case unit tests, "
            "complexity analysis and a review of numerical pitfalls. Return code and explanation "
            "in your answer. Do not use tools, read files or execute anything."
        )
    return (
        f"{secrets.token_hex(12)} | {domain} | {json.dumps(series)}\n"
        f"These are synthetic daily counts starting on a Monday. Use the values to {question}. "
        "Write a detailed analyst report with calculations, caveats, a forecast and a validation plan."
    )


def allocation(c, prompt, available, rates):
    # Byte bound for a byte-level tokenizer plus room for chat message framing.
    prompt_bound = len(prompt.encode()) + 64
    output = c["max_output"]
    if c["mode"] == "tokens":
        output = min(output, available - prompt_bound)
    else:
        left = available - prompt_bound * rates[0]
        if left < 0:
            return None
        if rates[2]:
            output = min(output, left // rates[2])
    if output < (1 if c.get("api_style", "chat") == "chat" else 16):
        return None
    return int(output), prompt_bound + output, cost(prompt_bound, 0, output, rates)


def usage(data):
    if not isinstance(data, dict):
        raise TypeError("接口没有返回有效的 usage")
    responses = "input_tokens" in data
    prompt = data.get("input_tokens" if responses else "prompt_tokens")
    output = data.get("output_tokens" if responses else "completion_tokens")

    def count(n):
        return type(n) is int and 0 <= n <= 10**8

    if not count(prompt) or not count(output) or not prompt + output:
        raise ValueError("接口没有返回有效的输入与输出用量")
    details = (
        data.get("input_tokens_details" if responses else "prompt_tokens_details") or {}
    )
    cached = (
        details.get("cached_tokens", data.get("prompt_cache_hit_tokens"))
        if isinstance(details, dict)
        else None
    )
    if cached is not None and (not count(cached) or cached > prompt):
        raise ValueError("缓存用量无效")
    return prompt, cached, output
