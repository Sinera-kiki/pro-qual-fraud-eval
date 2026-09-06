#!/usr/bin/env python3
"""
每周数据还原脚本：把三组抽样（大盘/定向加抽行业）与标注结果匹配，输出 Excel，并上传到看板作为产物保留。

用法：
    cd ${PROJECT_ROOT}
    python weekly_data_restore.py --week-dir wXX --run-id N

依赖文件（在 week-dir 下）：
  - overall_sample.csv      大盘抽样
  - industry_a_sample.csv      定向加抽行业抽样
  - industry_b_sample.csv   定向加抽行业抽样
  - annotated_wXX.csv       标注结果（或 annotated_*.csv，自动匹配最新的 annotated_*.csv）
  - sample_expanded_clusters.csv  含 is_sample_user 标记的匹配扩簇表

输出文件：
  - WXX_抽样标注匹配结果_MMDD-MMDD.xlsx  （三个 sheet）
  - 上传到看板 run_id 的 calc_metric 阶段产物

口径：
  - 标注结果按 user_id 去重，同一用户有多行时优先保留"实锤造假"
  - 抽样表 left join 去重后的标注结果
  - 加 in_cluster 列：标记该抽样账户是否在 sample_expanded_clusters 的 is_sample_user=True 中
  - 浓度 = 实锤造假数 / in_cluster=True 的账户数（与看板 metrics 口径一致）
"""

import argparse
import glob
import json
import os
import sys
import subprocess
import pandas as pd

DASHBOARD_BASE = "https://dashboard.example.com/pro-qual-eval"
PUSH_TOKEN = os.environ.get("DASH_PUSH_TOKEN", "")


def find_annotated_file(week_dir):
    """自动查找标注结果文件：优先 annotated_wXX.csv，否则取最新的 annotated_*.csv"""
    # 尝试匹配 annotated_wXX.csv
    candidates = sorted(glob.glob(os.path.join(week_dir, "annotated_w*.csv")), reverse=True)
    if candidates:
        return candidates[0]
    # 退而求其次：任意 annotated_*.csv
    candidates = sorted(glob.glob(os.path.join(week_dir, "annotated_*.csv")), reverse=True)
    if candidates:
        return candidates[0]
    return None


def load_and_dedup_annotated(ann_path):
    """读取标注结果，按 user_id 去重（优先保留实锤造假）"""
    ann = pd.read_csv(ann_path, dtype=str)
    # 口径对齐 metrics（2026-08-24 修正）：违规 = 实锤+疑似；去重优先级实锤 > 疑似 > 不违规
    ann["_priority"] = ann["remark_first"].map({"实锤造假": 0, "疑似造假": 1, "不违规": 2}).fillna(3)
    ann = ann.sort_values("_priority")
    ann_dedup = ann.drop_duplicates(subset="user_id", keep="first").drop(columns=["_priority"])
    return ann_dedup


def load_sample_expanded_uids(week_dir):
    """从 sample_expanded_clusters.csv 中提取 is_sample_user=True 的 user_id 集合"""
    expanded_path = os.path.join(week_dir, "sample_expanded_clusters.csv")
    if not os.path.exists(expanded_path):
        print(f"⚠️  未找到 sample_expanded_clusters.csv，in_cluster 列将全为 False")
        return set()
    expanded = pd.read_csv(expanded_path, dtype=str)
    sample_uids = set(expanded[expanded["is_sample_user"] == "True"]["user_id"])
    return sample_uids


def merge_and_calc(sample_df, ann_dedup, expanded_uids, name):
    """匹配抽样表与标注结果，加 in_cluster 列"""
    merged = sample_df.merge(ann_dedup, on="user_id", how="left")
    merged["in_cluster"] = merged["user_id"].isin(expanded_uids)

    has_remark = merged[merged["remark_first"].notna()]
    fake = (has_remark["remark_first"] == "实锤造假").sum()
    suspect = (has_remark["remark_first"] == "疑似造假").sum()
    not_fake = (has_remark["remark_first"] == "不违规").sum()
    denominator = merged["in_cluster"].sum()

    # 口径开关：hammer=分子只算实锤（W<week> 起默认）；both=实锤+疑似（W<week> 及以前）
    scope = os.environ.get("VIOLATION_SCOPE", "hammer")
    if scope == "both":
        numer = int(fake + suspect); scope_label = "实锤+疑似"
    else:
        numer = int(fake); scope_label = "实锤"

    print(f"\n{name}:")
    print(f"  抽样总量: {len(merged)}")
    print(f"  匹配到聚簇表(分母): {denominator}")
    print(f"  违规(分子, 口径={scope_label}): {numer}（实锤 {fake} + 疑似 {suspect}）")
    if denominator > 0:
        print(f"  违规浓度: {numer}/{denominator} = {numer/denominator*100:.2f}%")
    print(f"  匹配到标注结果: {len(has_remark)} (实锤 {fake}, 疑似 {suspect}, 不违规 {not_fake})")
    print(f"  未匹配到标注: {merged['remark_first'].isna().sum()}")

    return merged, {
        "name": name,
        "sample_total": len(merged),
        "denominator": int(denominator),
        "fake": int(fake),
        "suspect": int(suspect),
        "not_fake": int(not_fake),
        "numerator": numer,
        "scope": scope_label,
        "concentration": round(numer / denominator, 4) if denominator > 0 else 0,
    }


def extract_period(week_dir):
    """从目录名 wXX 和文件名中提取周期字符串，如 0803-0809"""
    # 从 annotated 文件名提取
    import re
    annotated_files = glob.glob(os.path.join(week_dir, "annotated_w*.csv"))
    for f in sorted(annotated_files, reverse=True):
        m = re.search(r'(\d{4})-(\d{4})', os.path.basename(f))
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    # 从 sample_platform_upload 文件名提取
    upload_files = glob.glob(os.path.join(week_dir, "*_*.csv"))
    for f in upload_files:
        m = re.search(r'(\d{4})-(\d{4})', os.path.basename(f))
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return "unknown"


def extract_week_num(week_dir):
    """从目录名 wXX 提取周数"""
    import re
    m = re.search(r'w(\d+)', week_dir, re.IGNORECASE)
    return m.group(1) if m else "XX"


def upload_to_dashboard(run_id, stage_code, file_path):
    """上传产物文件到看板"""
    url = f"{DASHBOARD_BASE}/api/push/runs/{run_id}/artifacts?stage_code={stage_code}"
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-H", f"X-Push-Token: {PUSH_TOKEN}",
        "-F", f"file=@{file_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    print(f"\n📤 上传到看板: {os.path.basename(file_path)} → run_id={run_id}, stage={stage_code}")
    print(f"  响应: {result.stdout[:200]}")
    if result.returncode != 0:
        print(f"  ⚠️ curl 返回码 {result.returncode}")
        print(f"  stderr: {result.stderr[:200]}")
    return result.returncode == 0


def mark_stage(run_id, stage_code, status, message=""):
    """标记阶段状态"""
    url = f"{DASHBOARD_BASE}/api/push/runs/{run_id}/stages/{stage_code}/mark"
    payload = json.dumps({"status": status, "message": message})
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-H", f"X-Push-Token: {PUSH_TOKEN}",
        "-H", "Content-Type: application/json",
        "-d", payload,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="每周数据还原：抽样 × 标注匹配 + 看板上传")
    parser.add_argument("--week-dir", required=True, help="周目录，如 w<week>")
    parser.add_argument("--run-id", type=int, required=True, help="看板 run_id")
    parser.add_argument("--no-upload", action="store_true", help="只生成 Excel，不上传看板")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    week_dir = os.path.join(base_dir, args.week_dir)
    if not os.path.isdir(week_dir):
        print(f"❌ 目录不存在: {week_dir}")
        sys.exit(1)

    # 1. 找标注结果文件
    ann_path = find_annotated_file(week_dir)
    if not ann_path:
        print(f"❌ 未在 {week_dir} 找到标注结果文件 (annotated_*.csv)")
        sys.exit(1)
    print(f"📄 标注结果: {os.path.basename(ann_path)}")

    # 2. 加载数据
    ann_dedup = load_and_dedup_annotated(ann_path)
    print(f"标注结果去重后: {len(ann_dedup)} 行")
    print(f"remark_first 分布: {ann_dedup['remark_first'].value_counts(dropna=False).to_dict()}")

    expanded_uids = load_sample_expanded_uids(week_dir)
    print(f"聚簇表中 is_sample_user=True 的 user_id: {len(expanded_uids)} 个")

    overall = pd.read_csv(os.path.join(week_dir, "overall_sample.csv"), dtype=str)
    industry_a = pd.read_csv(os.path.join(week_dir, "industry_a_sample.csv"), dtype=str)
    industry_b = pd.read_csv(os.path.join(week_dir, "industry_b_sample.csv"), dtype=str)

    # 3. 匹配
    overall_m, overall_stats = merge_and_calc(overall, ann_dedup, expanded_uids, "大盘抽样")
    industry_a_m, industry_a_stats = merge_and_calc(industry_a, ann_dedup, expanded_uids, "定向加抽行业抽样")
    industry_b_m, industry_b_stats = merge_and_calc(industry_b, ann_dedup, expanded_uids, "定向加抽行业抽样")

    # 4. 输出 Excel
    week_num = extract_week_num(week_dir)
    period = extract_period(week_dir)
    output_name = f"W{week_num}_抽样标注匹配结果_{period}.xlsx"
    output_path = os.path.join(week_dir, output_name)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # 用户 2026-08-25 要求：无资质图账户（in_cluster=False，聚簇表查不到）不写入交付表，
        # 避免用数的人误把空行算进去。浓度统计已在 merge_and_calc 内按全量抽样算完，不受影响。
        overall_m[overall_m["in_cluster"]].to_excel(writer, sheet_name="大盘抽样", index=False)
        industry_a_m[industry_a_m["in_cluster"]].to_excel(writer, sheet_name="定向加抽行业抽样", index=False)
        industry_b_m[industry_b_m["in_cluster"]].to_excel(writer, sheet_name="定向加抽行业抽样", index=False)

    print(f"\n✅ 已输出: {output_path} ({os.path.getsize(output_path)/1024:.1f} KB)")

    # 5. 上传到看板
    if not args.no_upload:
        # 标记 calc_metric 阶段为 running
        mark_stage(args.run_id, "calc_metric", "running", "数据还原：匹配抽样与标注结果")

        success = upload_to_dashboard(args.run_id, "calc_metric", output_path)

        summary = (
            f"数据还原完成（W{week_num}, {period}）\n"
            f"大盘: {overall_stats['numerator']}/{overall_stats['denominator']} = {overall_stats['concentration']*100:.2f}%\n"
            f"定向加抽行业: {industry_a_stats['numerator']}/{industry_a_stats['denominator']} = {industry_a_stats['concentration']*100:.2f}%\n"
            f"定向加抽行业: {industry_b_stats['numerator']}/{industry_b_stats['denominator']} = {industry_b_stats['concentration']*100:.2f}%\n"
            f"（分子口径={overall_stats['scope']}）\n"
            f"产物文件: {output_name}"
        )

        if success:
            mark_stage(args.run_id, "calc_metric", "done", summary)
            print(f"\n📊 看板已更新 (run_id={args.run_id}, stage=calc_metric)")
        else:
            mark_stage(args.run_id, "calc_metric", "failed", "产物上传失败")
            print(f"\n⚠️ 看板上传失败，Excel 已生成在本地")

        # 也上传到 write_result 阶段作为保留文件
        upload_to_dashboard(args.run_id, "write_result", output_path)
    else:
        print(f"\n⏭️  --no-upload 模式，未上传看板")

    print(f"\n=== 浓度汇总（分子口径={overall_stats['scope']}） ===")
    for s in [overall_stats, industry_a_stats, industry_b_stats]:
        print(f"  {s['name']}: {s['numerator']}/{s['denominator']} = {s['concentration']*100:.2f}%")


if __name__ == "__main__":
    main()
