#!/usr/bin/env python3
"""行业维度分布推送（一级 + 二级）。

推送三组分布到看板：
  - industry_baseline / industry_baseline_l2
      入驻账户基数、进入标注池的账户数、被实锤的账户数。
      分母是标注池而不是全量入驻——展示的是"算法准确率"，不是"造假浓度"。
  - sample_distribution / sample_distribution_l2
      抽样分布，只含随机抽样样本（不含定向加抽），保证代表性检验有效。
  - violation_distribution / violation_distribution_l2
      违规账户在各行业的分布。依赖人工标注结果。

行业名沿用上游表原始字段，不做映射改写；接口是快照覆盖，必须一次推全部行业。
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
    ap.add_argument("--period", required=True, help='如 "0727-0802"')
    ap.add_argument("--full-file", required=True, help="全量 embedding（含行业与资质图）")
    ap.add_argument("--cluster-file", required=True, help="全量聚簇结果")
    ap.add_argument("--suspect-file", required=True, help="可疑簇列表")
    ap.add_argument("--overall-sample", required=True, help="全行业随机抽样")
    ap.add_argument("--annotate-pool", help="上传标注平台的池子 CSV；annotated_uid 的来源")
    ap.add_argument("--annotated", help="标注结果 CSV；给了就推 confirmed_fake_uid")
    ap.add_argument("--no-suspected", action="store_true",
                    help="不推 suspected_fake_uid。默认推——v1.0.48 文档标注为"
                         "「视图暂不使用」而非废弃，留着不影响，缺了将来要重推")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # ---------- 读入基础数据 ----------
    # 账户 → (一级行业, 二级行业)；同账户多行取首次出现值
    ind1, ind2 = {}, {}
    for r in read_rows(args.full_file):
        uid = r["user_id"]
        if uid not in ind1:
            ind1[uid] = r.get("trade_first_name", "").strip()
            ind2[uid] = r.get("trade_second_name", "").strip()

    suspect_cids = {int(r["cluster_id"]) for r in read_rows(args.suspect_file)}

    clustered, suspected = set(), set()
    for r in read_rows(args.cluster_file):
        cid = int(r["cluster_id"])
        if cid < 0:
            continue
        uid = r["user_id"]
        clustered.add(uid)
        if cid in suspect_cids:
            suspected.add(uid)
        ind1.setdefault(uid, r.get("trade_first_name", "").strip())
        ind2.setdefault(uid, r.get("trade_second_name", "").strip())

    registered = set(ind1)

    # ---------- 标注池 / 人工实锤（算法准确率视图用）----------
    # annotated_uid 的定义是「进入人工标注池的账户数」= 上传给标注平台那个文件里的账户，
    # 不是「标注结果表里出现过的账户」。标注未跑完时两者不等，用结果表当分母会把
    # 准确率算高（分母偏小）。故 annotated_uid 取自 --annotate-pool。
    pool_uids, confirmed_uids = set(), set()
    if args.annotate_pool:
        pool_uids = {r["user_id"].strip() for r in read_rows(args.annotate_pool)}
    if args.annotated:
        for r in read_rows(args.annotated):
            if (r.get("remark_first") or "").strip() == "实锤造假":
                confirmed_uids.add(r["user_id"].strip())
        # 实锤账户必须落在标注池内，否则说明池子文件与标注结果不是同一批
        stray = confirmed_uids - pool_uids if pool_uids else set()
        if stray:
            print(f"⚠️  {len(stray)} 个实锤账户不在标注池文件内，"
                  f"池子文件与标注结果可能不是同一版，请核对后再推")

    want_susp = not args.no_suspected
    want_pool = bool(args.annotate_pool)
    want_cfm = bool(args.annotated)

    def new_bucket():
        d = {"registered_uid": 0, "clustered_uid": 0}
        if want_susp:
            d["suspected_fake_uid"] = 0
        if want_pool:
            d["annotated_uid"] = 0
        if want_cfm:
            d["confirmed_fake_uid"] = 0
        return d

    def build(bucket, uid):
        if want_susp and uid in suspected:
            bucket["suspected_fake_uid"] += 1
        if want_pool and uid in pool_uids:
            bucket["annotated_uid"] += 1
        if want_cfm and uid in confirmed_uids:
            bucket["confirmed_fake_uid"] += 1

    # ---------- industry_baseline（一级）----------
    b1 = defaultdict(new_bucket)
    for uid in registered:
        k = ind1.get(uid) or "未知"
        b1[k]["registered_uid"] += 1
        if uid in clustered:
            b1[k]["clustered_uid"] += 1
        build(b1[k], uid)
    items_b1 = [{"industry": k, **v} for k, v in sorted(b1.items(),
                key=lambda kv: -kv[1]["registered_uid"])]

    # ---------- industry_baseline_l2（二级）----------
    b2 = defaultdict(new_bucket)
    for uid in registered:
        k = (ind1.get(uid) or "未知", ind2.get(uid) or "未知")
        b2[k]["registered_uid"] += 1
        if uid in clustered:
            b2[k]["clustered_uid"] += 1
        build(b2[k], uid)
    items_b2 = [{"first_industry": a, "second_industry": b, **v}
                for (a, b), v in sorted(b2.items(), key=lambda kv: -kv[1]["registered_uid"])]

    # ---------- sample_distribution（一级 / 二级）----------
    # 只取全行业随机抽样，且必须落在聚簇表内（与看板口径一致）
    sample_uids = [r["user_id"] for r in read_rows(args.overall_sample)]
    in_cluster_tbl = set(ind1)
    hit = [u for u in sample_uids if u in in_cluster_tbl]

    s1 = defaultdict(int)
    s2 = defaultdict(int)
    for uid in hit:
        s1[ind1.get(uid) or "未知"] += 1
        s2[(ind1.get(uid) or "未知", ind2.get(uid) or "未知")] += 1
    items_s1 = [{"industry": k, "count": v} for k, v in sorted(s1.items(), key=lambda kv: -kv[1])]
    items_s2 = [{"first_industry": a, "second_industry": b, "count": v}
                for (a, b), v in sorted(s2.items(), key=lambda kv: -kv[1])]

    print(f"全行业抽样 {len(sample_uids)}，匹配到聚簇表 {len(hit)}")
    print(f"industry_baseline      : {len(items_b1)} 个一级行业，registered 合计 {len(registered)}")
    if want_pool:
        ann = sum(v["annotated_uid"] for v in b1.values())
        print(f"  标注池账户合计 {ann}（池子文件去重 {len(pool_uids)}）")
        if want_cfm and ann:
            cfm = sum(v["confirmed_fake_uid"] for v in b1.values())
            print(f"  人工实锤合计 {cfm}，算法准确率 {cfm / ann:.1%}")
            for k in ("定向加抽行业", "SPECIAL_INDUSTRY_A"):
                v = b1.get(k)
                if v and v["annotated_uid"]:
                    print(f"    {k}: 标注池 {v['annotated_uid']} / 实锤 "
                          f"{v['confirmed_fake_uid']} = "
                          f"{v['confirmed_fake_uid'] / v['annotated_uid']:.1%}")
    else:
        print("  annotated_uid / confirmed_fake_uid: 未传（标注未完成）")
    if not want_susp:
        print("  suspected_fake_uid: 未传")
    print(f"industry_baseline_l2   : {len(items_b2)} 个二级行业")
    print(f"sample_distribution    : {len(items_s1)} 个一级行业，合计 {sum(s1.values())}")
    print(f"sample_distribution_l2 : {len(items_s2)} 个二级行业，合计 {sum(s2.values())}")

    if args.dry_run:
        print("\n[dry-run] 未推送")
        return

    print()
    for path, payload in [
        (f"/api/push/runs/{args.run_id}/industry_baseline",
         {"period": args.period, "items": items_b1}),
        (f"/api/push/runs/{args.run_id}/industry_baseline_l2",
         {"period": args.period, "items": items_b2}),
        (f"/api/push/runs/{args.run_id}/sample_distribution",
         {"items": items_s1}),
        (f"/api/push/runs/{args.run_id}/sample_distribution_l2",
         {"items": items_s2}),
    ]:
        status, resp = post(path, payload)
        print(f"  {status}  {path.rsplit('/', 1)[-1]:24s} count={resp.get('count')}")


if __name__ == "__main__":
    main()
