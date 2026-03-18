"""
大模型语义判断 - 硅基流动 / OpenAI 兼容

Speed optimisation: sends multiple texts per API call and runs
several calls concurrently with asyncio.gather.
"""
import asyncio
import json
from openai import AsyncOpenAI

SYSTEM_PROMPT = (
    "你是一个舆情分析助手。用户会给你多条内容（用 [1] [2] … 编号），"
    "请逐条判断情感，返回一个 JSON 数组，每个元素格式："
    '{\"n\":编号, \"sentiment\":\"正面|中性|负面\", \"remark\":\"简短备注\"}。'
    "只返回 JSON 数组，不要加其他文字。"
)

SYSTEM_PROMPT_SINGLE = (
    "你是一个舆情分析助手。对用户给出的内容进行语义判断，"
    '只回复JSON格式：{"sentiment":"正面|中性|负面", "remark":"简短备注"}'
)

CHUNK = 5          # texts per API call
CONCURRENCY = 4    # parallel API calls


async def sentiment_analyze(
    base_url: str,
    api_key: str,
    model: str,
    texts: list[str],
) -> list[dict]:
    """Batch sentiment analysis with concurrency."""
    if not api_key or not model:
        return [{"sentiment": "neutral", "remark": "未配置API"} for _ in texts]

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    if len(texts) == 1:
        return [await _single(client, model, texts[0])]

    chunks: list[tuple[int, list[str]]] = []
    for i in range(0, len(texts), CHUNK):
        chunks.append((i, texts[i:i + CHUNK]))

    sem = asyncio.Semaphore(CONCURRENCY)

    async def run_chunk(start: int, chunk: list[str]) -> list[dict]:
        async with sem:
            return await _batch(client, model, start, chunk)

    tasks = [run_chunk(s, c) for s, c in chunks]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[dict] = [{"sentiment": "中性", "remark": ""} for _ in texts]
    for (start, chunk), result in zip(chunks, chunk_results):
        if isinstance(result, Exception):
            for j in range(len(chunk)):
                out[start + j] = {"sentiment": "中性", "remark": f"调用失败: {result}"}
        else:
            for j, r in enumerate(result):
                if start + j < len(out):
                    out[start + j] = r
    return out


async def _batch(client, model, start, chunk: list[str]) -> list[dict]:
    """Send multiple texts in one API call."""
    numbered = "\n".join(
        f"[{i + 1}] {t[:400]}" for i, t in enumerate(chunk)
    )
    try:
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
    except Exception as e:
        return [{"sentiment": "中性", "remark": f"调用失败: {e}"}] * len(chunk)


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
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
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
