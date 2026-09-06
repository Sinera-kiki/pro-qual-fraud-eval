#!/usr/bin/env python3
"""
违规行业分布推送（一级 + 二级）

对接《看板 push 接口协议 v1》(vX 版)：
  violation_distribution / violation_distribution_l2

口径要点：
  - 只统计「全行业随机抽样 ∩ 当周全量聚簇表」的那批账户，与 sample_distribution 严格同源，
    不含定向加抽行业定向加抽，否则违规率分母分子对不上。
  - 账号级去重：一个账号任一张图被标为违规即计一次违规。
  - **违规判定看 remark_first，不看 image_label**：remark_first 为空的记录是标注平台的
    默认填充（image_label 一律「违规」、annotator_email 为空），主体是 cluster_id=-1 的
    未成簇账户，标注员根本没打开过。按 image_label 采信会让未成簇账户全部变违规，
    浓度虚高数倍（2026-08-04 实测：641 vs 正确的 94）。
  - 同簇扩展账户只是给标注员看的参考，不参与分子。
  - detail.real / suspect 取标注 remark_first 的「实锤造假 / 疑似造假」；
    proxy（资质挂靠）当前标注体系无此类别，恒为 0。
  - 行业名用聚簇表原始 trade_first_name / trade_second_name，不做映射改写。
  - 快照式覆盖，一次带全。
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


def post(path: str, payload: dict) -> tuple[int, dict]:
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


def read_rows(path):
    with open(path, encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--cluster-file", required=True, help="全量聚簇结果（行业来源 + 分母范围）")
    ap.add_argument("--overall-sample", required=True, help="全行业随机抽样")
    ap.add_argument("--annotated", required=True, help="标注结果 CSV")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ind1, ind2 = {}, {}
    for r in read_rows(args.cluster_file):
        uid = r["user_id"].strip()
        if uid not in ind1:
            ind1[uid] = (r.get("trade_first_name") or "").strip()
            ind2[uid] = (r.get("trade_second_name") or "").strip()

    sample = [r["user_id"].strip() for r in read_rows(args.overall_sample)]
    denom = [u for u in sample if u in ind1]

    # 口径开关：hammer=只算实锤（W<week> 起默认）；both=实锤+疑似（W<week> 及以前）
    import os
    scope = os.environ.get("VIOLATION_SCOPE", "hammer")
    viol_marks = {"实锤造假"} if scope == "hammer" else {"实锤造假", "疑似造假"}
    violated, real, suspect = set(), set(), set()
    skipped_default = 0
    for r in read_rows(args.annotated):
        uid = r["user_id"].strip()
        mark = (r.get("remark_first") or "").strip()
        if not mark:
            # remark_first 为空 = 标注平台的默认填充，不是人工判断结果。
            # 这类记录 image_label 一律是「违规」，且 annotator_email 为空，
            # 主体是 cluster_id=-1 的未成簇账户（标注员根本没打开过）。
            # 若按 image_label 采信，未成簇账户会全部变违规，浓度虚高数倍。
            skipped_default += 1
            continue
        if mark in viol_marks:
            violated.add(uid)
            (real if mark == "实锤造假" else suspect).add(uid)
    suspect -= real  # 同账号既有实锤又有疑似时归为实锤
    if skipped_default:
        print(f"排除标注平台默认值 {skipped_default} 行（remark_first 为空、无标注人）")

    hit = [u for u in denom if u in violated]

    v1 = defaultdict(lambda: {"violated_count": 0, "real": 0, "suspect": 0})
    v2 = defaultdict(lambda: {"violated_count": 0, "real": 0, "suspect": 0})
    for uid in hit:
        for bucket, key in ((v1, ind1.get(uid) or "未知"),
                            (v2, (ind1.get(uid) or "未知", ind2.get(uid) or "未知"))):
            bucket[key]["violated_count"] += 1
            if uid in real:
                bucket[key]["real"] += 1
            elif uid in suspect:
                bucket[key]["suspect"] += 1

    items_v1 = [{"industry": k, "violated_count": v["violated_count"],
                 "detail": {"real": v["real"], "suspect": v["suspect"], "proxy": 0}}
                for k, v in sorted(v1.items(), key=lambda kv: -kv[1]["violated_count"])]
    items_v2 = [{"first_industry": a, "second_industry": b, "violated_count": v["violated_count"],
                 "detail": {"real": v["real"], "suspect": v["suspect"], "proxy": 0}}
                for (a, b), v in sorted(v2.items(), key=lambda kv: -kv[1]["violated_count"])]

    print(f"全行业抽样 {len(sample)}，落在聚簇表内（分母）{len(denom)}，判违规（分子）{len(hit)}")
    print(f"violation_distribution    : {len(items_v1)} 个一级行业，合计 {sum(x['violated_count'] for x in items_v1)}")
    print(f"violation_distribution_l2 : {len(items_v2)} 个二级行业，合计 {sum(x['violated_count'] for x in items_v2)}")

    if args.dry_run:
        print("\n[dry-run] 未推送")
        return

    print()
    for path, payload in [
        (f"/api/push/runs/{args.run_id}/violation_distribution", {"items": items_v1}),
        (f"/api/push/runs/{args.run_id}/violation_distribution_l2", {"items": items_v2}),
    ]:
        status, resp = post(path, payload)
        print(f"  {status}  {path.rsplit('/', 1)[-1]:26s} count={resp.get('count')}")


if __name__ == "__main__":
    main()
