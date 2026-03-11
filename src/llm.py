"""
大模型语义判断 - 硅基流动 / OpenAI 兼容
"""
import json
from openai import AsyncOpenAI
from typing import Optional


async def sentiment_analyze(
    base_url: str,
    api_key: str,
    model: str,
    texts: list[str],
) -> list[dict]:
    """批量语义判断：正面/中性/负面 + 备注"""
    if not api_key or not model:
        return [{"sentiment": "neutral", "remark": "未配置API"} for _ in texts]

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    results = []
    for text in texts:
        content = text[:500] if len(text) > 500 else text  # 限制长度
        try:
            r = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个舆情分析助手。对用户给出的内容进行语义判断，只回复JSON格式：{\"sentiment\":\"正面|中性|负面\", \"remark\":\"简短备注\"}",
                    },
                    {"role": "user", "content": content},
                ],
                temperature=0.3,
            )
            raw = r.choices[0].message.content or "{}"
            # 尝试解析 JSON
            if "{" in raw:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                obj = json.loads(raw[start:end])
                results.append({
                    "sentiment": obj.get("sentiment", "中性"),
                    "remark": obj.get("remark", ""),
                })
            else:
                results.append({"sentiment": "中性", "remark": raw[:100]})
        except Exception as e:
            results.append({"sentiment": "中性", "remark": f"解析失败: {e}"})

    return results
