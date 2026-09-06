#!/usr/bin/env python3
"""
全行业抽样误差档位自动选择器

背景（2026-08-03 用户定的运营规则）：
  待标注的**命中簇数**应保持在 600~700 左右，且后续会逐渐减少。
  若命中簇数超过 750，就把全行业抽样误差 e 放宽到 3% 左右来压降规模。

本脚本把这条规则做成自动决策，并且——关键——**在跑抽样 SQL 之前就能预测**：
  Hive 侧全行业抽样是 `ORDER BY crc32(concat(user_id,'_seed42'))` 取前 N，
  这个排序可以用 Python zlib.crc32 完美复现（2026-08-03 实测：抽中账户的
  crc32 最大值 < 未抽中账户的最小值，排序完全一致）。
  因此只要有了全量聚簇结果，就能对任意样本量 N 预测其命中簇数，
  一次选定误差档，不必"跑完发现超标再重抽"。

用法（在聚簇完成后、跑全行业抽样 SQL 之前调用）：
  python pick_sampling_error.py \
    --cluster-file w<week>/<file> \
    --big-n 15017 \
    --p 0.1353 \
    [--fixed-sample-file w<week>/<file> \
    [--fixed-sample-file w<week>/<file> \
    [--json-out w<week>/<file>

输出：选定的 e、样本量 n（直接填进抽样 SQL 的 `WHERE rnd_all <= n`）、
      预测命中簇数，以及各候选档位的扫描表。

决策逻辑：
  1. 先按既有规则取默认误差 e0（P<30% → 2%；P>=30% → 5%，见
     semantic_sample_size_error_margin.md）。
  2. 用 e0 预测命中簇数。若 <= 750，直接用 e0，不改动既有口径。
  3. 若 > 750，按用户指定放宽到 3%（PREFERRED_E）。
  4. 若 3% 仍压不到 750 以下，继续放宽到能压住的最小误差档。
  5. 若连最宽档位都超 750，如实告警，不静默放行。

注意：只调全行业抽样的误差。定向加抽行业是定向加抽，样本量本就很小
（各几十~几百），不参与本规则。
"""
import argparse
import csv
import json
import math
import os
import sys
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calc_sample_size import calc_n, conf_to_z, default_e  # noqa: E402

# 用户 2026-08-03 定的运营目标
TARGET_LOW = 600
TARGET_HIGH = 700
HARD_CEILING = 750
PREFERRED_E = 0.03   # 超阈值时优先放宽到这一档（用户原话："放至 3% 左右"）

# 候选误差档位（由精到粗）
CANDIDATE_E = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]

SALT = "_seed42"


def load_cluster(path):
    """读全量聚簇结果 → (账户→所属有效簇集合, 全部账户)"""
    uid_cids = defaultdict(set)
    all_uids = set()
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        need = {"user_id", "cluster_id"}
        missing = need - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"[FATAL] 聚簇表缺列 {missing}: {path}")
        for row in reader:
            uid = row["user_id"]
            all_uids.add(uid)
            cid = int(row["cluster_id"])
            if cid >= 0:
                uid_cids[uid].add(cid)
    return uid_cids, all_uids


def load_fixed(paths):
    """定向加抽账户（定向加抽行业），恒定进入样本，不随全行业误差变化"""
    uids = set()
    for p in paths or []:
        with open(p, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if "user_id" not in (reader.fieldnames or []):
                raise SystemExit(f"[FATAL] 抽样表缺 user_id: {p}")
            for row in reader:
                uid = (row.get("user_id") or "").strip()
                if uid:
                    uids.add(uid)
    return uids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-file", required=True)
    ap.add_argument("--big-n", type=float, required=True, help="全行业大 N")
    ap.add_argument("--p", type=float, required=True, help="上周大盘浓度（小数）")
    ap.add_argument("--conf", type=float, default=0.99)
    ap.add_argument("--fixed-sample-file", action="append",
                    help="定向加抽表（定向加抽行业），可重复传")
    ap.add_argument("--salt", default=SALT, help="全行业抽样盐值，默认 _seed42")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    uid_cids, all_uids = load_cluster(args.cluster_file)
    fixed = load_fixed(args.fixed_sample_file)

    # 复现 Hive 的 ORDER BY crc32(concat(user_id, salt))
    def h(uid):
        return zlib.crc32((uid + args.salt).encode("utf-8"))
    ordered = sorted(all_uids, key=h)

    def predict_hit_clusters(n):
        picked = set(ordered[:n]) | fixed
        hit = set()
        for u in picked:
            hit |= uid_cids.get(u, set())
        return len(hit)

    z = conf_to_z(args.conf)

    def n_for(e):
        return math.ceil(calc_n(z, args.p, e, args.big_n))

    # 扫描候选档
    scan = []
    for e in CANDIDATE_E:
        n = n_for(e)
        scan.append({"e": e, "sample_size": n, "predicted_hit_clusters": predict_hit_clusters(n)})

    # --- 决策 ---
    e0 = default_e(args.p)
    base = next((s for s in scan if abs(s["e"] - e0) < 1e-9), None)
    if base is None:
        n0 = n_for(e0)
        base = {"e": e0, "sample_size": n0, "predicted_hit_clusters": predict_hit_clusters(n0)}
        scan.append(base)
        scan.sort(key=lambda s: s["e"])

    if base["predicted_hit_clusters"] <= HARD_CEILING:
        chosen = base
        reason = (f"默认误差 {e0*100:.1f}% 预测命中簇 {base['predicted_hit_clusters']}，"
                  f"未超阈值 {HARD_CEILING}，沿用默认口径")
    else:
        # 用户 2026-08-03 明确：超阈值就"放至 3% 左右"。优先锚定 3%。
        anchor = next((s for s in scan if abs(s["e"] - PREFERRED_E) < 1e-9), None)
        if anchor and anchor["predicted_hit_clusters"] <= HARD_CEILING:
            chosen = anchor
            reason = (f"默认误差 {e0*100:.1f}% 预测命中簇 {base['predicted_hit_clusters']} "
                      f"> {HARD_CEILING}，按既定规则放宽至 {PREFERRED_E*100:.0f}%"
                      f"（预测 {anchor['predicted_hit_clusters']} 簇）")
        else:
            # 3% 仍压不到阈值以下，继续放宽：取能压到 HARD_CEILING 以下的最小误差
            under = [s for s in scan if s["predicted_hit_clusters"] <= HARD_CEILING]
            if under:
                chosen = min(under, key=lambda s: s["e"])
                reason = (f"默认误差 {e0*100:.1f}% 预测 {base['predicted_hit_clusters']} > {HARD_CEILING}，"
                          f"且 {PREFERRED_E*100:.0f}% 仍有 "
                          f"{anchor['predicted_hit_clusters'] if anchor else 'NA'} 簇；"
                          f"继续放宽至 {chosen['e']*100:.1f}%（预测 {chosen['predicted_hit_clusters']}）")
            else:
                chosen = scan[-1]
                reason = (f"[告警] 所有候选档位预测命中簇均 > {HARD_CEILING}，"
                          f"最宽档 {chosen['e']*100:.1f}% 仍有 {chosen['predicted_hit_clusters']} 簇，"
                          f"需人工介入（可能本周资质图复用异常集中）")

    result = {
        "big_n": args.big_n,
        "p": args.p,
        "conf": args.conf,
        "default_e": e0,
        "chosen_e": chosen["e"],
        "chosen_sample_size": chosen["sample_size"],
        "predicted_hit_clusters": chosen["predicted_hit_clusters"],
        "target_range": [TARGET_LOW, TARGET_HIGH],
        "hard_ceiling": HARD_CEILING,
        "adjusted": abs(chosen["e"] - e0) > 1e-9,
        "reason": reason,
        "scan": scan,
    }

    print("=== 候选档位扫描（全行业）===")
    for s in scan:
        mark = ""
        if abs(s["e"] - chosen["e"]) < 1e-9:
            mark = "  ← 选定"
        elif abs(s["e"] - e0) < 1e-9:
            mark = "  (默认档)"
        flag = "" if s["predicted_hit_clusters"] <= HARD_CEILING else "  ⚠超阈值"
        print(f"  e={s['e']*100:4.1f}%   样本量 {s['sample_size']:5d}   "
              f"预测命中簇 {s['predicted_hit_clusters']:4d}{flag}{mark}")
    print()
    print(f"决策：e = {chosen['e']*100:.1f}%，样本量 = {chosen['sample_size']}，"
          f"预测命中簇 = {chosen['predicted_hit_clusters']}")
    print(f"依据：{reason}")
    print()
    print(f"→ 抽样 SQL 里把 `WHERE rnd_all <= N` 的 N 改为 {chosen['chosen_sample_size'] if False else chosen['sample_size']}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"→ {args.json_out}")


if __name__ == "__main__":
    main()
