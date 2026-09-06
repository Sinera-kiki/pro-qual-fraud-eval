#!/usr/bin/env python3
"""
把某个 biz_date 的已有产物补推到审核看板（五节点 + 产物 + 台账）。

用法：
    python3 backfill_dashboard.py --biz-date 2026-08-03 \
        --cluster-file cmp_20260803/B_cluster.csv \
        --task-file daily/20260803/annotation_task.csv \
        [--run-id manual-20260803] [--dry-run]

用于历史补数与联调验证；日跑主流程直接在各节点调 dashboard_client。
"""
import argparse
import os
from datetime import datetime, timezone

import pandas as pd

from dashboard_client import DashboardClient


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_time(path):
    return iso(datetime.fromtimestamp(os.path.getmtime(path), timezone.utc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--biz-date", required=True)
    ap.add_argument("--cluster-file", required=True)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--fetch-file",
                    help="取数输入池 CSV（已瘦身、不含 embeds 列），挂到 fetch 节点")
    ap.add_argument("--run-id")
    ap.add_argument("--dataset-id")
    ap.add_argument("--dataset-name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    nodash = args.biz_date.replace("-", "")
    run_id = args.run_id or f"backfill-{nodash}"

    print("读取产物…")
    clust = pd.read_csv(args.cluster_file)
    clust["is_target_user"] = (clust["is_target_user"].astype(str).str.lower()
                               .isin(["true", "1"]))
    task = pd.read_csv(args.task_file)
    task["is_target_user"] = (task["is_target_user"].astype(str).str.lower()
                              .isin(["true", "1"]))

    c = clust[clust["cluster_id"] >= 0]
    cu = c.groupby("cluster_id")["user_id"].nunique()

    m_fetch = {
        "input_rows": len(clust),
        "week_user_count": int(clust["user_id"].nunique()),
        "target_user_count": int(clust[clust["is_target_user"]]["user_id"].nunique()),
    }
    # 待审核簇 / 待审核账户以「实际上传标注平台的任务表」为准，
    # 与看板卡片口径一致（接口文档 c70ad5f9… 第二节）。
    task_clusters = int(task["cluster_id"].nunique())
    task_target_users = int(task[task["is_target_user"]]["user_id"].nunique())

    m_cluster = {
        # 看板卡片「待审核簇」认这个 key，必须带 _count 后缀
        "valid_cluster_count": task_clusters,
        # 看板卡片「待审核账户」认这个 key；缺失时服务端会拿簇数兜底，
        # 导致卡片与风险检出率双双偏低，务必显式上报
        "target_hit_user_count": task_target_users,
        "all_cluster": int(cu.size),
        "single_user_cluster": int((cu < 2).sum()),
    }
    m_upload = {
        "rows": len(task),
        "valid_cluster_count": task_clusters,
        "target_hit_user_count": task_target_users,
        "cluster_count": task_clusters,
        "task_users": int(task["user_id"].nunique()),
        "task_target_users": task_target_users,
    }

    print(f"  fetch  : {m_fetch}")
    print(f"  cluster: {m_cluster}")
    print(f"  upload : {m_upload}")
    if args.dry_run:
        print("dry-run，未推送")
        return

    dc = DashboardClient(biz_date=args.biz_date, run_id=run_id)
    t_cluster = file_time(args.cluster_file)
    t_task = file_time(args.task_file)

    dc.run_report(status="fetching", note="历史补推")

    for key, metrics, ts in [("fetch", m_fetch, t_cluster),
                             ("cluster", m_cluster, t_cluster),
                             ("upload", m_upload, t_task)]:
        dc._started[key] = (0, ts)
        dc.step_finish(key, metrics=metrics, status="success")

    dc._started["notify"] = (0, t_task)
    dc.step_finish("notify", metrics={"chat": "（补推，未实际通知）"},
                   status="success")

    dc._started["await_review"] = (0, t_task)
    dc.step_finish("await_review",
                   metrics={"annotated": 0, "total": m_upload["cluster_count"],
                            "progress": 0},
                   status="running")

    print("上传产物…")
    # 接口文档要求 fetch/cluster/upload 各挂自己的产物。
    # fetch 的输入池 CSV 含 embeds 列（数十 MB~GB），需先瘦身再传，
    # 这里只在调用方显式提供 --fetch-file 时上传。
    if args.fetch_file:
        dc.upload_artifact("fetch", args.fetch_file,
                           filename=f"input_pool_{nodash}.csv")
    dc.upload_artifact("cluster", args.cluster_file,
                       filename=f"cluster_result_{nodash}.csv")
    dc.upload_artifact("upload", args.task_file,
                       filename=f"annotation_task_{nodash}.csv")

    dc.run_report(status="uploaded",
                  dataset_id=args.dataset_id,
                  dataset_name=args.dataset_name,
                  total_rows=m_upload["rows"],
                  cluster_count=m_upload["cluster_count"],
                  uploaded_at=t_task, note="历史补推")

    got = dc.get_run()
    print(f"\n回读：status={got['run']['status']} "
          f"steps={len(got.get('steps', []))} "
          f"artifacts={len(got.get('artifacts', []))}")
    ov = got.get("overview") or {}
    print(f"overview={ov}")


if __name__ == "__main__":
    main()
