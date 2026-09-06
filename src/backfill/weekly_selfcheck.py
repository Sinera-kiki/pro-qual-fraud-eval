#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_selfcheck.py — 资质造假评估周度链路自查

在 calc_metric / 推看板之后运行。把历史上真实踩过的坑做成断言，
每周自动跑一遍，有问题主动报给用户，而不是等用户发现。

用法：
  python weekly_selfcheck.py --week-dir w<week> --run-id 4 \
      --annotated w<week>/<file>

退出码：0 全部通过；1 有 FAIL（须报告用户并停止后续动作）；2 只有 WARN。

检查项（每条都对应一次真实事故，改动前先读 Why）：
  C1 标注文件真实性     未标注默认值防护：image_label 单一取值 / remark 全空
  C2 标注池 vs 结果表   行数、账户集合一致
  C3 浓度三组重算       与看板 metrics 逐组比对
  C4 分布合计对齐       sample/violation 分布合计 == 大盘分母/分子（一级+二级）
  C5 基线逻辑自洽       annotated_uid <= clustered_uid（子集不得大于父集）
  C6 标注池分母口径     annotated_uid 必须只数成簇账户，不含 cluster_id=-1
  C7 跨周可比性         与上周准确率口径一致，口径变更须显式提示
  C8 cert_method 自洽   无 registered=0 的幽灵行；分子 ≤ 分母（子集 ≤ 父集）
"""
import argparse
import csv
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict

DASH = "https://dashboard.example.com/pro-qual-eval"
TOKEN = os.environ.get("DASH_PUSH_TOKEN", "")
CA = "${HOME}/.openclaw/mitm-proxy/ca-cert.pem"

RESULTS = []


def record(level, code, title, detail=""):
    RESULTS.append((level, code, title, detail))
    mark = {"PASS": "  OK ", "FAIL": " FAIL", "WARN": " WARN"}[level]
    print(f"[{mark}] {code} {title}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")


def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def get_run(run_id):
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(CA)
    req = urllib.request.Request(
        f"{DASH}/api/runs/{run_id}", headers={"X-Push-Token": TOKEN})
    with urllib.request.urlopen(req, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-dir", required=True, help="如 w<week>")
    ap.add_argument("--run-id", type=int, required=True, help="看板 run id")
    ap.add_argument("--prev-run-id", type=int, help="上周 run id，用于跨周可比性检查")
    ap.add_argument("--annotated", required=True, help="标注结果 CSV")
    ap.add_argument("--pool", help="上传标注平台的池子 CSV；缺省自动在周目录找")
    args = ap.parse_args()

    wd = args.week_dir
    ann = read_csv(args.annotated)

    pool_path = args.pool
    if not pool_path:
        cands = [f for f in os.listdir(wd) if f.startswith("商家账号资质聚簇") and f.endswith(".csv")]
        if len(cands) != 1:
            sys.exit(f"周目录下找不到唯一的标注池文件，请用 --pool 指定：{cands}")
        pool_path = os.path.join(wd, cands[0])
    pool = read_csv(pool_path)
    full = read_csv(os.path.join(wd, "v2_full_cluster_result.csv"))

    print(f"周目录 {wd}｜标注池 {os.path.basename(pool_path)}｜"
          f"标注结果 {os.path.basename(args.annotated)}\n")

    # ---------- C1 标注文件真实性 ----------
    # Why: force=true 能下到未标完的数据，image_label 会全是"违规"（平台默认值），
    #      拿去算浓度会得到 ~100% 的假结果。2026-07-31 实测。
    real = [r for r in ann if r["cluster_id"] != "-1"]
    labels = set(r["image_label"] for r in real)
    has_remark = any(r["remark_first"].strip() for r in real)
    if len(labels) <= 1 or not has_remark:
        record("FAIL", "C1", "标注文件疑似未标注默认值",
               f"image_label 取值 {labels}；remark 全空={not has_remark}\n"
               f"→ 不要用这份数据算浓度，先查 progress 的 is_completed")
    else:
        record("PASS", "C1", f"标注文件真实（{len(real)} 行已标，标签 {sorted(labels)}）")

    # ---------- C2 标注池 vs 结果表 ----------
    if len(pool) != len(ann):
        record("FAIL", "C2", "标注池与结果表行数不一致",
               f"池子 {len(pool)} 行 vs 结果 {len(ann)} 行")
    elif set(r["user_id"] for r in pool) != set(r["user_id"] for r in ann):
        record("FAIL", "C2", "标注池与结果表账户集合不一致（可能不是同一批）")
    else:
        record("PASS", "C2", f"标注池与结果表一致（{len(pool)} 行）")

    # ---------- C3 浓度三组重算 ----------
    full_users = set(r["user_id"] for r in full)
    # 2026-08-24 定稿：分子只算 remark_first='实锤造假'（旧口径 image_label='违规' 已废弃）
    viol_users = set(r["user_id"] for r in real if r["remark_first"].strip() == "实锤造假")
    groups = [("整体样本", "overall_sample.csv"),
              ("SPECIAL_INDUSTRY_A样本", "industry_a_sample.csv"),
              ("定向加抽行业行业样本", "industry_b_sample.csv")]
    recalc = {}
    for label, f in groups:
        users = set(r["user_id"] for r in read_csv(os.path.join(wd, f)))
        denom = users & full_users          # 分母：有资质图数据的抽样账户
        numer = denom & viol_users
        recalc[label] = (len(denom), len(numer),
                         round(len(numer) / len(denom), 4) if denom else 0)

    run = get_run(args.run_id)
    board = {m["sample_group"]: m["concentration"] for m in run["metrics"]}
    bad = []
    for label, (d, n, rate) in recalc.items():
        b = board.get(label)
        if b is None or abs(b - rate) > 1e-4:
            bad.append(f"{label}: 重算 {rate:.4f}（{n}/{d}） vs 看板 {b}")
    if bad:
        record("FAIL", "C3", "浓度与看板不一致", "\n".join(bad))
    else:
        record("PASS", "C3", "三组浓度与看板一致",
               "；".join(f"{k} {v[2]:.2%}（{v[1]}/{v[0]}）" for k, v in recalc.items()))

    # ---------- C4 分布合计对齐 ----------
    # Why: sample_distribution 只能用全行业随机抽样，混入定向加抽会让行业占比失真。
    ov_d, ov_n, _ = recalc["整体样本"]
    for key, expect, name in [("sample_distribution", ov_d, "大盘分母"),
                              ("violation_distribution", ov_n, "大盘分子"),
                              ("sample_distribution_l2", ov_d, "大盘分母"),
                              ("violation_distribution_l2", ov_n, "大盘分子")]:
        rows = run.get(key) or []
        col = "count" if "sample" in key else "violated_count"
        got = sum(r.get(col) or 0 for r in rows)
        if got != expect:
            record("FAIL", "C4", f"{key} 合计对不上",
                   f"合计 {got} != {name} {expect}（可能混入了定向加抽行业定向加抽）")
        else:
            record("PASS", "C4", f"{key} 合计 {got} == {name}")

    # ---------- C5 基线逻辑自洽 ----------
    # Why: annotated_uid 是 clustered_uid 的子集，大于就是口径出错。
    for key, namer in [("industry_baseline", lambda r: r["industry"]),
                       ("industry_baseline_l2",
                        lambda r: f"{r.get('first_industry')}/{r.get('second_industry')}")]:
        rows = run.get(key) or []
        bad = [namer(r) for r in rows
               if (r.get("annotated_uid") or 0) > (r.get("clustered_uid") or 0)]
        if bad:
            record("FAIL", "C5", f"{key} 有 annotated_uid > clustered_uid",
                   f"{len(bad)}/{len(rows)} 条异常，如 {bad[:6]}\n"
                   f"→ 子集不可能大于父集，检查 annotated_uid 是否混入未成簇账户")
        else:
            record("PASS", "C5", f"{key} 基线逻辑自洽（{len(rows)} 条）")

    # ---------- C6 标注池分母口径 ----------
    # Why: 2026-08-11 事故。池子里 cluster_id=-1 的未成簇账户是为了"文件内抽样账户数
    #      == 浓度分母"能直接对账才写进去的，平台不让标、实际一个都没标，
    #      不能计入算法准确率的分母 annotated_uid。
    pool_all = set(r["user_id"] for r in pool)
    pool_clustered = set(r["user_id"] for r in pool if r["cluster_id"] != "-1")
    unstannable = pool_all - pool_clustered
    board_ann = sum((r.get("annotated_uid") or 0)
                    for r in (run.get("industry_baseline") or []))
    if unstannable and board_ann == len(pool_all):
        cfm = sum((r.get("confirmed_fake_uid") or 0)
                  for r in (run.get("industry_baseline") or []))
        record("FAIL", "C6", "annotated_uid 混入了未成簇账户",
               f"池子 {len(pool_all)} 账户，其中 cluster_id=-1 未成簇 {len(unstannable)} 个"
               f"（平台不让标）\n"
               f"看板 annotated_uid 合计 {board_ann}，应为 {len(pool_clustered)}\n"
               f"准确率被低估：{cfm}/{board_ann}={cfm/board_ann:.1%} → "
               f"应为 {cfm}/{len(pool_clustered)}={cfm/len(pool_clustered):.1%}")
    elif board_ann == len(pool_clustered):
        record("PASS", "C6", f"annotated_uid 口径正确（{board_ann} = 池内成簇账户）")
    else:
        record("WARN", "C6", "annotated_uid 与两种口径都对不上",
               f"看板 {board_ann}｜池子全量 {len(pool_all)}｜池内成簇 {len(pool_clustered)}")

    # ---------- C7 跨周可比性 ----------
    if args.prev_run_id:
        prev = get_run(args.prev_run_id)
        pa = sum((r.get("annotated_uid") or 0) for r in (prev.get("industry_baseline") or []))
        pc = sum((r.get("confirmed_fake_uid") or 0) for r in (prev.get("industry_baseline") or []))
        cur_c = sum((r.get("confirmed_fake_uid") or 0)
                    for r in (run.get("industry_baseline") or []))
        if pa and board_ann:
            prev_acc, cur_acc = pc / pa, cur_c / board_ann
            note = (f"上周 {prev_acc:.1%}（{pc}/{pa}）｜本周 {cur_acc:.1%}"
                    f"（{cur_c}/{board_ann}）")
            if abs(cur_acc - prev_acc) > 0.10:
                record("WARN", "C7", "算法准确率跨周波动 >10pp，先确认口径未变再解读",
                       note)
            else:
                record("PASS", "C7", "跨周准确率波动在合理区间", note)
    else:
        record("WARN", "C7", "未传 --prev-run-id，跳过跨周可比性检查")

    # ---------- C8 cert_method 自洽（2026-08-21 新增） ----------
    # 历史事故：分子含历史扩散账户但分母只当周入驻 → registered=0 violated>0 幽灵行；
    #          分子分母不同 biz_org_info 分区 → 冒出分母没有的 authentication_type。
    cert_items = run.get("cert_method") or []
    if not cert_items:
        record("WARN", "C8", "看板 cert_method 为空——本周未推或推送失败")
    else:
        # C8a：每个 industry 内不得出现 registered=0 且 violated>0 的行（幽灵行）
        ghosts = [it for it in cert_items
                  if int(it.get("registered_count", 0)) == 0
                  and int(it.get("violated_count", 0)) > 0]
        if ghosts:
            record("FAIL", "C8a", "cert_method 有 registered=0 但 violated>0 的幽灵行",
                   "\n".join(f"  {g['industry']} / {g['method']}: r=0 v={g['violated_count']}"
                             for g in ghosts) +
                   "\n  → 分子分母未同分区，或分子含历史扩散账户，重跑 push_cert_method.py")
        else:
            record("PASS", "C8a", f"cert_method 无幽灵行（{len(cert_items)} 条 items 全部 r≥v 或 r>0）")

        # C8b：每个 industry 内分子合计不得超过分母合计（子集 ≤ 父集）
        by_ind = defaultdict(lambda: {"r": 0, "v": 0})
        for it in cert_items:
            by_ind[it["industry"]]["r"] += int(it.get("registered_count", 0))
            by_ind[it["industry"]]["v"] += int(it.get("violated_count", 0))
        bad = [(ind, d) for ind, d in by_ind.items() if d["v"] > d["r"]]
        if bad:
            record("FAIL", "C8b", "cert_method 分子超过分母（子集 > 父集，逻辑破产）",
                   "\n".join(f"  {ind}: 分母 {d['r']} < 分子 {d['v']}" for ind, d in bad))
        else:
            record("PASS", "C8b", f"cert_method 分子 ≤ 分母（{len(by_ind)} 个行业）",
                   " / ".join(f"{ind}: {d['v']}/{d['r']}" for ind, d in by_ind.items()))

    # ---------- 汇总 ----------
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print(f"\n{'=' * 60}")
    print(f"自查完成：{len(RESULTS)} 项，FAIL {len(fails)}，WARN {len(warns)}")
    if fails:
        print("\n须报告用户并暂停后续动作：")
        for _, code, title, _ in fails:
            print(f"  · {code} {title}")
        sys.exit(1)
    sys.exit(2 if warns else 0)


if __name__ == "__main__":
    main()
