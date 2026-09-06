#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日跑防呆校验（两道防线）

用法：
  1) 提交取数前：  python daily_guard.py --sql daily/{{ds_1_days_ago_nodash}}/q.sql --biz-date {{ds_1_days_ago}}
  2) 取数落盘后：  python daily_guard.py --csv daily/{{ds_1_days_ago_nodash}}/day_embedding.csv --biz-date {{ds_1_days_ago}}

防线1（--sql）：q.sql 中所有 pro_settle_date = 'X' 的 X 必须等于业务日期，
                且所有 dtm = 'Y' 的 Y 必须等于业务日期去横线版。不一致 → exit 1。
防线2（--csv）：与前一日的 day_embedding.csv 对比去重 user_id 集合，
                完全相同 → 高概率取错日期 → exit 2。前一日文件不存在则跳过。
"""
import sys, csv, re, argparse, pathlib

def guard_sql(sql_path, biz_date):
    txt = pathlib.Path(sql_path).read_text(encoding="utf-8")
    biz_nodash = biz_date.replace("-", "")
    bad = []
    for m in re.finditer(r"pro_settle_date\s*=\s*'([0-9-]+)'", txt):
        if m.group(1) != biz_date:
            bad.append(f"pro_settle_date='{m.group(1)}' != 业务日期 {biz_date}")
    for m in re.finditer(r"dtm\s*=\s*'([0-9]+)'", txt):
        if m.group(1) != biz_nodash:
            bad.append(f"dtm='{m.group(1)}' != 分区日期 {biz_nodash}")
    if bad:
        print("❌ SQL 日期校验失败：")
        for b in bad: print("  -", b)
        sys.exit(1)
    print(f"✅ SQL 日期校验通过（pro_settle_date 与 dtm 均为 {biz_date}）")

def users_of(path):
    us = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("user_id"): us.add(row["user_id"])
    return us

def guard_csv(csv_path, biz_date):
    cur = pathlib.Path(csv_path).resolve()
    siblings = sorted(p for p in cur.parent.parent.glob("*/day_embedding.csv") if p != cur)
    prev = siblings[-1] if siblings else None
    if not prev or not prev.exists():
        print("⚠️ 找不到前一日 day_embedding.csv，跳过重复性校验")
        return
    a, b = users_of(prev), users_of(cur)
    if a and a == b:
        print(f"❌ 与前一日 {prev.parent.name} 的用户集合完全相同（{len(a)} 户），高概率取错日期，禁止继续！")
        sys.exit(2)
    inter = len(a & b)
    print(f"✅ 重复性校验通过：本次 {len(b)} 户，前一日 {len(a)} 户，重叠 {inter} 户")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql"); ap.add_argument("--csv"); ap.add_argument("--biz-date", required=True)
    a = ap.parse_args()
    if a.sql: guard_sql(a.sql, a.biz_date)
    if a.csv: guard_csv(a.csv, a.biz_date)
