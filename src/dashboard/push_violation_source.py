#!/usr/bin/env python3
"""
实锤造假来源分布推送（violation_source，接口文档第十四节，2026-08-18 新增）

口径：
  - 分子 = 当周标注结果 remark_first='实锤造假' 的去重 user_id（含同簇扩展账户，
    与 industry_baseline 的 confirmed_fake_uid 同口径：标注池里全部实锤，不只抽样种子）。
  - 来源 = 治理宽表 dws_ecm_pro_account_qualification_full_governance_df 的 apply_source：
      SELLER_PASS -> 号店入驻(shop_count)【定稿口径】
      AD_PASS     -> 号广入驻(ad_count)
      PC/APP及其余(OFFICIAL/OFFLINE/PROFESSION/空) -> 商家账号入驻(professional_count)【2026-08-19 定稿：SELLER_PASS=号店、AD_PASS=号广、PC和APP=商家账号】
  - 匹配不到宽表的实锤账户只计入大盘 detail.missing，不计入任何行业。
  - 行业名用 trade_first_name 原值；另推一条 "大盘整体" 汇总。
  - 快照式覆盖，一次带全。

用法：
  python push_violation_source.py --run-id N \
      --annotated wXX/annotated_real.csv \
      --source-file wXX/violation_source_raw.csv   # 事先用 direct-engine 查好的 行业,apply_source,uid_cnt
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
    ap.add_argument("--annotated", required=True, help="标注结果 CSV（remark_first 判实锤）")
    ap.add_argument("--source-file", required=True,
                    help="direct-engine 导出的 行业,apply_source,uid_cnt 三列 CSV（实锤 uid 在治理宽表中的分布）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 1) 实锤 uid
    confirmed = set()
    with open(args.annotated, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("remark_first") or "").strip() == "实锤造假":
                confirmed.add(r["user_id"].strip())
    print(f"实锤 uid：{len(confirmed)}")

    # 2) 行业×来源分布（source-file 是按实锤 uid 聚合好的，直接信任其计数）
    agg = defaultdict(lambda: {"professional_count": 0, "shop_count": 0, "ad_count": 0})
    total_matched = 0
    with open(args.source_file, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ind = (r["first_trade_name"] or "").strip() or "未知"
            src = (r["apply_source"] or "").strip()
            n = int(r["uid_cnt"])
            row = agg[ind]
            if src == SHOP:
                row["shop_count"] += n
            elif src == AD:
                row["ad_count"] += n
            else:
                row["professional_count"] += n
            total_matched += n

    missing = len(confirmed) - total_matched
    overall = {"professional_count": 0, "shop_count": 0, "ad_count": 0}
    for v in agg.values():
        for k in overall:
            overall[k] += v[k]

    items = [{"industry": "大盘整体", **overall, "detail": {"missing": max(missing, 0)}}]
    items += [{"industry": k, **v}
              for k, v in sorted(agg.items(),
                                 key=lambda kv: -(kv[1]["professional_count"] + kv[1]["shop_count"] + kv[1]["ad_count"]))]

    print(f"匹配到来源 {total_matched}，宽表缺失 {missing}")
    print(f"大盘：商家账号 {overall['professional_count']} / 号店 {overall['shop_count']} / 号广 {overall['ad_count']}")
    print(f"共 {len(items)} 条（含大盘整体）")

    if args.dry_run:
        print("\n[dry-run] 未推送")
        return

    status, resp = post(f"/api/push/runs/{args.run_id}/violation_source", {"items": items})
    print(f"\nviolation_source -> HTTP {status}: {json.dumps(resp, ensure_ascii=False)[:300]}")
    if status != 200:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
