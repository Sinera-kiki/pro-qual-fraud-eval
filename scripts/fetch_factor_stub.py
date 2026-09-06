#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_factor 接口桩（可读版 / Interface Stub）

生产环境依赖内部 risk-hitlog-factor skill 的 fetch_factor 模块，用来查询
风控命中日志（audit hit log）里某个账号的「相似资质检索因子」与处置结果。
本仓库以这个桩文件文档化该接口契约，方便外部读者理解下游调用，不依赖内部 skill。

接口契约（weekly_hammer_audit_compare.py 实际只用到这三个函数）：

1. load_cookie() -> str
   读取调用内部风控接口所需的 SSO cookie。
   实现：优先读环境变量 SSO_COOKIE_FILE 指向的文件（JSON），否则返回空串。

2. parse_time(s) -> int
   把时间字符串转成毫秒时间戳。

3. call(cond: dict, cookie: str) -> dict
   POST 查询风控命中日志，返回 {"data": {"records": [...]}}。
   cond 关键字段：
     - includedScenarioId: 审核场景（例 professionalAccountAudit / componentAudit-HG）
     - showFactors: 要拉取的因子，本例 ["proAccountQualificationSimResList"]
     - userId: 目标账号
     - startTime / endTime: 查询时间窗（毫秒）
   records 每条结构：
     - businessId / auditStartTime / scenarioId：工单号、审核时间、场景
     - hitProcessRecords[]：命中的策略动作（scenarioProcessName/strategyName）与标签（jsonData.tagName）
     - showFactorValues["proAccountQualificationSimResList"]：相似检索因子，
       JSON 数组，元素含 query_img_url(送审图) / sim_img_url(命中底图) /
       pixel_similarity / cosine_similarity / type_name
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime


def load_cookie() -> str:
    """读取 SSO cookie。优先读 SSO_COOKIE_FILE 环境变量指向的 JSON 文件。"""
    path = os.environ.get("SSO_COOKIE_FILE", "")
    if path:
        p = os.path.expanduser(path)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            # 不同环境 cookie 字段名可能不同，这里兼容常见写法
            return data.get("cookie") or data.get("Cookie") or data.get("raw") or ""
    return ""


def parse_time(s) -> int:
    """时间字符串 → 毫秒时间戳。支持 '2026-08-10' 或 ISO 格式。"""
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s!r}")


def call(cond: dict, cookie: str) -> dict:
    """查询风控命中日志（桩实现，真实环境替换为内部 HTTP 接口）。

    未配置内部接口地址时，返回空 records，让上游脚本以「因子无值」路径继续跑，
    便于本地试跑与阅读逻辑，不会因缺依赖而中断。
    """
    endpoint = os.environ.get("RISK_HITLOG_API", "")
    if not endpoint:
        return {"data": {"records": []}}
    # 真实环境：POST {endpoint}/<query> 带 cookie，此处仅留调用位
    # import requests
    # resp = requests.post(endpoint, json=cond, headers={"Cookie": cookie}, verify=False)
    # return resp.json()
    return {"data": {"records": []}}