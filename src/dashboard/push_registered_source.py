#!/usr/bin/env python3
"""
新入驻账户来源分布推送（registered_source，2026-08-20 新增）

接口：POST /api/push/runs/{run_id}/registered_source
      Header: X-Push-Token（同其他 push 接口）
      快照式覆盖：同 run_id 重推先删后插，必须一次带全所有行业。

Payload：
  {
    "items": [
      { "industry": "大盘整体",
        "professional_count": N, "shop_count": N, "ad_count": N },
      { "industry": "<trade_first_name 原值>", ... },
      ...
    ]
  }

口径：
  - 分子 = 当周新入驻账户去重 user_id（initAudit 通过 + 认证类型 2/4，
    与 semantic_new_settle_account_scope 一致）。
  - 来源来自入驻宽表 apply_source（2026-08-19 定稿）：
      SELLER_PASS -> shop_count       （号店）
      AD_PASS     -> ad_count         （号广）
      PC / APP    -> professional_count（商家账号，两个入口合并）
      其余枚举（OFFICIAL/OFFLINE/MERCHANT/RELATED_EMPLOYEE/空）-> professional_count
        （沿用 push_violation_source.py 的兜底逻辑，避免漏计）
  - 行业名一律用 trade_first_name 原值，不做映射改写。
  - "大盘整体" 一条汇总所有行业。

用法：
  python push_registered_source.py --run-id N \
      --source-file wXX/registered_source_raw.csv   # 事先用 direct-engine 查好
      [--dry-run]

source-file 三列（列名严格）：first_trade_name, apply_source, uid_cnt
   —— 由 SQL 直接按（trade_first_name, apply_source）分组 count(distinct user_id) 生成。
"""
import os
import argparse
import csv
import json
import ssl
import urllib.request
from collections import defaultdict

BASE = "https://dashboard.example.com/pro-qual-eval"
TOKEN = os.environ.get("DASH_PUSH_TOKEN", "")
CA = "${HOME}/.openclaw/mitm-proxy/ca-cert.pem"

SHOP, AD = "SELLER_PASS", "AD_PASS"


def post(path: str, payload: dict):
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(CA)
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"X-Push-Token": TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--source-file", required=True,
                    help="direct-engine 导出的 first_trade_name,apply_source,uid_cnt 三列 CSV")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 按 (行业, apply_source) 聚合到三桶
    agg = defaultdict(lambda: {"professional_count": 0, "shop_count": 0, "ad_count": 0})
    total = 0
    with open(args.source_file, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ind = (r.get("first_trade_name") or "").strip() or "未知"
            src = (r.get("apply_source") or "").strip()
            n = int(r["uid_cnt"])
            row = agg[ind]
            if src == SHOP:
                row["shop_count"] += n
            elif src == AD:
                row["ad_count"] += n
            else:
                # PC / APP / OFFICIAL / OFFLINE / MERCHANT / RELATED_EMPLOYEE / 空 -> 商家账号
                row["professional_count"] += n
            total += n

    # 大盘整体
    overall = {"professional_count": 0, "shop_count": 0, "ad_count": 0}
    for v in agg.values():
        for k in overall:
            overall[k] += v[k]

    items = [{"industry": "大盘整体", **overall}]
    items += [{"industry": k, **v}
              for k, v in sorted(
                  agg.items(),
                  key=lambda kv: -(kv[1]["professional_count"]
                                   + kv[1]["shop_count"]
                                   + kv[1]["ad_count"]))]

    print(f"新入驻账户合计 {total}")
    print(f"大盘：商家账号 {overall['professional_count']} / "
          f"号店 {overall['shop_count']} / 号广 {overall['ad_count']}")
    print(f"共 {len(items)} 条（含大盘整体，行业 {len(agg)} 个）")

    if args.dry_run:
        print("\n[dry-run] 未推送。样例前 3 条：")
        for it in items[:3]:
            print(f"  {it}")
        return

    status, resp = post(f"/api/push/runs/{args.run_id}/registered_source",
                        {"items": items})
    print(f"\nregistered_source -> HTTP {status}: "
          f"{json.dumps(resp, ensure_ascii=False)[:300]}")
    if status != 200:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
