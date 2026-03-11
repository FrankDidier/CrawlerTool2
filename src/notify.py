"""
消息推送 - 钉钉 / 企业微信
"""
import requests
from typing import Optional


def send_dingtalk(webhook_url: str, title: str, text: str) -> bool:
    if not webhook_url:
        return False
    try:
        r = requests.post(
            webhook_url,
            json={"msgtype": "text", "text": {"content": f"{title}\n{text}"}},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def send_wechat(webhook_url: str, title: str, text: str) -> bool:
    """企业微信机器人"""
    if not webhook_url:
        return False
    try:
        r = requests.post(
            webhook_url,
            json={
                "msgtype": "text",
                "text": {"content": f"{title}\n{text}"},
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False
