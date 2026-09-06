#!/usr/bin/env python3
"""
生成标注任务表（annotation_task.csv）

筛选条件（三条同时满足）：
  1. cluster_id >= 0（排除未成簇噪声）
  2. 簇内至少一个目标日新户（is_target_user=true）
  3. 簇内去重账号数 >= 2 —— 具体口径由 --min-user-mode 控制

--min-user-mode 两档（对应实时拦截上线前后）：
  all（默认，实时拦截上线前）
      簇内去重「全部账号」数 >= 2。
      保留「1 个新户 + N 个历史账号」的跨日挂靠簇，覆盖全部风险。
  target（实时拦截上线后）
      簇内去重「目标日新户」数 >= 2。
      只保留当天新户互撞的簇；跨日挂靠已由第一道防线实时拦截，不再重复送标注。

08-03 实测差异（7 天池口径）：all → 710 个待标注新户 / 476 簇；
      target → 652 个 / 311 簇。两档差集只是「1 新户 + N 历史账号」那类簇，
      不要按「历史挂靠占 76%」外推，实测差异远小于该推法。
切换时机：日跑自 2026-08-18 起取数改为仅前一天，链路固定用 target。
"""
import argparse
import sys

import pandas as pd

NEEDED = ["user_id", "qualification_url", "cluster_id",
          "trade_first_name", "trade_second_name", "is_target_user"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-file", required=True, help="聚簇结果 CSV")
    ap.add_argument("--output", required=True, help="标注任务 CSV 输出路径")
    ap.add_argument("--min-user-mode", choices=["all", "target"], default="all",
                    help="簇内账号数下限的计数口径：all=全部账号（默认，"
                         "实时拦截上线前）；target=仅目标日新户（上线后）")
    ap.add_argument("--min-users", type=int, default=2, help="账号数下限，默认 2")
    ap.add_argument("--stats-json", help="把统计结果另存为 JSON")
    args = ap.parse_args()

    df = pd.read_csv(args.cluster_file)
    missing = [c for c in NEEDED if c not in df.columns]
    if missing:
        sys.exit(f"聚簇结果缺少必需列：{missing}")

    df["is_target_user"] = (df["is_target_user"].astype(str).str.lower()
                            .isin(["true", "1"]))
    df["cluster_id"] = pd.to_numeric(df["cluster_id"], errors="coerce").fillna(-1).astype(int)

    total_rows = len(df)
    total_users = df["user_id"].nunique()
    total_targets = df[df["is_target_user"]]["user_id"].nunique()

    # 条件 1
    c = df[df["cluster_id"] >= 0]
    all_clusters = c["cluster_id"].nunique()

    # 条件 2：簇内含目标日新户
    has_target = c.groupby("cluster_id")["is_target_user"].any()
    target_cluster_ids = set(has_target[has_target].index)

    # 条件 3：账号数下限（按口径切换计数对象）
    if args.min_user_mode == "target":
        counted = (c[c["is_target_user"]].groupby("cluster_id")["user_id"].nunique())
    else:
        counted = c.groupby("cluster_id")["user_id"].nunique()
    enough_ids = set(counted[counted >= args.min_users].index)

    keep_ids = target_cluster_ids & enough_ids
    task = c[c["cluster_id"].isin(keep_ids)].copy()

    dropped_no_target = len(target_cluster_ids ^ (target_cluster_ids & enough_ids))
    stats = {
        "min_user_mode": args.min_user_mode,
        "min_users": args.min_users,
        "input_rows": total_rows,
        "input_users": total_users,
        "input_target_users": total_targets,
        "all_clusters": int(all_clusters),
        "clusters_with_target": len(target_cluster_ids),
        "task_clusters": len(keep_ids),
        "task_rows": len(task),
        "task_users": int(task["user_id"].nunique()) if len(task) else 0,
        "task_target_users": int(task[task["is_target_user"]]["user_id"].nunique()) if len(task) else 0,
    }
    stats["dropped_clusters"] = stats["clusters_with_target"] - stats["task_clusters"]

    task[NEEDED].to_csv(args.output, index=False, encoding="utf-8-sig")

    print(f"口径: min-user-mode={args.min_user_mode} (>= {args.min_users})")
    print(f"输入: {total_rows} 行 / {total_users} 账号 / 其中目标日新户 {total_targets}")
    print(f"有效簇: {all_clusters} → 含新户 {len(target_cluster_ids)} "
          f"→ 满足账号数下限 {len(keep_ids)}（剔除 {stats['dropped_clusters']}）")
    print(f"输出: {args.output}")
    print(f"  待标注 {stats['task_rows']} 行 / {stats['task_users']} 账号 "
          f"/ 其中目标日新户 {stats['task_target_users']}")

    if args.stats_json:
        import json
        with open(args.stats_json, "w") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"统计已存: {args.stats_json}")

    if not len(task):
        print("⚠️ 无待标注簇，上游应标记 skipped 且不上传标注平台")


if __name__ == "__main__":
    main()
