"""
大模型语义判断 - 硅基流动 / OpenAI 兼容

Speed strategy:
  - Pack 25 texts into a single API call (numbered [1]…[25])
  - Run 2 concurrent calls to stay under rate limits
  - Auto-retry with exponential backoff on 429 / timeout
"""
import asyncio
import json
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是舆情分析助手。用户给出多条编号内容（[1] [2] …），"
    "逐条判断情感并返回 JSON 数组，格式："
    '[{"n":编号,"sentiment":"正面|中性|负面","remark":"简短备注"}]。'
    "只返回 JSON 数组，不要任何其他文字。"
)

SYSTEM_PROMPT_SINGLE = (
    "你是一个舆情分析助手。对用户给出的内容进行语义判断，"
    '只回复JSON格式：{"sentiment":"正面|中性|负面", "remark":"简短备注"}'
)

CHUNK = 25         # texts per single API call
CONCURRENCY = 2    # parallel API calls (low to avoid rate-limit)
MAX_RETRIES = 3    # retries per chunk on failure
RETRY_BASE = 3.0   # base delay in seconds (exponential backoff)


async def sentiment_analyze(
    base_url: str,
    api_key: str,
    model: str,
    texts: list[str],
) -> list[dict]:
    """Batch sentiment analysis with concurrency + retry."""
    if not api_key or not model:
        return [{"sentiment": "neutral", "remark": "未配置API"} for _ in texts]

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)

    if len(texts) == 1:
        return [await _single(client, model, texts[0])]

    chunks: list[tuple[int, list[str]]] = []
    for i in range(0, len(texts), CHUNK):
        chunks.append((i, texts[i:i + CHUNK]))

    sem = asyncio.Semaphore(CONCURRENCY)

    async def run_chunk(start: int, chunk: list[str]) -> list[dict]:
        async with sem:
            return await _batch_with_retry(client, model, chunk)

    tasks = [run_chunk(s, c) for s, c in chunks]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[dict] = [{"sentiment": "中性", "remark": ""} for _ in texts]
    for (start, chunk), result in zip(chunks, chunk_results):
        if isinstance(result, Exception):
            for j in range(len(chunk)):
                out[start + j] = {"sentiment": "中性",
                                  "remark": f"调用失败: {result}"}
        else:
            for j, r in enumerate(result):
                if start + j < len(out):
                    out[start + j] = r
    return out


async def _batch_with_retry(client, model, chunk: list[str]) -> list[dict]:
    """Call _batch with exponential-backoff retry on failure."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _batch(client, model, chunk)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            is_rate_limit = ("429" in err_str or "rate" in err_str
                             or "limit" in err_str or "too many" in err_str
                             or "timeout" in err_str or "timed out" in err_str)
            if is_rate_limit and attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE * (2 ** attempt)
                logger.warning(
                    "API rate-limit/timeout (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            elif attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1.0)
            else:
                break
    return [{"sentiment": "中性", "remark": f"调用失败: {last_exc}"}] * len(chunk)


async def _batch(client, model, chunk: list[str]) -> list[dict]:
    """Send multiple texts in one API call."""
    numbered = "\n".join(
        f"[{i + 1}] {t[:300]}" for i, t in enumerate(chunk)
    )
    r = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        temperature=0.3,
    )
    raw = r.choices[0].message.content or "[]"
    arr = _parse_array(raw)
    results: list[dict] = []
    for i in range(len(chunk)):
        matched = next((a for a in arr if a.get("n") == i + 1), None)
        if matched:
            results.append({
                "sentiment": matched.get("sentiment", "中性"),
                "remark": matched.get("remark", ""),
            })
        elif i < len(arr):
            results.append({
                "sentiment": arr[i].get("sentiment", "中性"),
                "remark": arr[i].get("remark", ""),
            })
        else:
            results.append({"sentiment": "中性", "remark": ""})
    return results


async def _single(client, model, text: str) -> dict:
    """Single-text analysis (used for API test button)."""
    content = text[:500]
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_SINGLE},
                {"role": "user", "content": content},
            ],
            temperature=0.3,
        )
        raw = r.choices[0].message.content or "{}"
        if "{" in raw:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            obj = json.loads(raw[start:end])
            return {
                "sentiment": obj.get("sentiment", "中性"),
                "remark": obj.get("remark", ""),
            }
        return {"sentiment": "中性", "remark": raw[:100]}
    except Exception as e:
        return {"sentiment": "中性", "remark": f"解析失败: {e}"}


def _parse_array(raw: str) -> list[dict]:
    """Extract a JSON array from potentially messy LLM output."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(
            lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        )
        raw = raw.strip()
    try:
        if "[" in raw:
            start = raw.index("[")
            end = raw.rindex("]") + 1
            return json.loads(raw[start:end])
    except Exception:
        pass
    objs = []
    idx = 0
    while True:
        try:
            s = raw.index("{", idx)
            e = raw.index("}", s) + 1
            objs.append(json.loads(raw[s:e]))
            idx = e
        except (ValueError, json.JSONDecodeError):
            break
    return objs
