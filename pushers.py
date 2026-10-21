# -*- coding: utf-8 -*-
"""
微信推送通道（均免费）：
- serverchan：Server酱（sct.ft08.com），推送到个人微信
- pushplus：PushPlus（pushplus.plus.vip? 实际 www.pushplus.plus）
- wecom：企业微信群机器人 Webhook
"""
import logging
import requests

log = logging.getLogger("floater.push")

TIMEOUT = 8
UA = {"User-Agent": "Mozilla/5.0"}


def push_serverchan(send_key, title, content):
    url = "https://sctapi.ft08.com/%s.send" % send_key
    r = requests.post(url, data={"title": title, "desp": content},
                      headers=UA, timeout=TIMEOUT)
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError("Server酱返回: %s" % j)
    return True


def push_pushplus(token, title, content):
    url = "http://www.pushplus.plus/send"
    r = requests.post(url, json={"token": token, "title": title,
                                 "content": content, "template": "markdown"},
                      headers=UA, timeout=TIMEOUT)
    j = r.json()
    if j.get("code") != 200:
        raise RuntimeError("PushPlus返回: %s" % j)
    return True


def push_wecom(webhook, title, content):
    md = "### %s\n%s" % (title, content.replace("\n", "\n\n"))
    r = requests.post(webhook, json={"msgtype": "markdown",
                                     "markdown": {"content": md}},
                      headers=UA, timeout=TIMEOUT)
    j = r.json()
    if j.get("errcode") != 0:
        raise RuntimeError("企业微信返回: %s" % j)
    return True


CHANNELS = {
    "serverchan": ("Server酱", push_serverchan, "SendKey"),
    "pushplus": ("PushPlus", push_pushplus, "Token"),
    "wecom": ("企业微信机器人", push_wecom, "Webhook 地址"),
}


def send(channel, key, title, content):
    """按渠道推送，成功返回 True，失败抛异常"""
    if channel not in CHANNELS:
        raise RuntimeError("未知推送渠道: %s" % channel)
    fn = CHANNELS[channel][1]
    return fn(key, title, content)


def test(channel, key):
    return send(channel, key, "盯盘小助手 · 测试推送",
                "这是一条测试消息。\n\n看到它说明推送通道配置成功。")
