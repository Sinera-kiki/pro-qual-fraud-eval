#!/usr/bin/env python3
"""
抽样账户命中全量聚簇结果 → 扩展同簇账号 → 生成待标注表

用法：
  python expand_sample_clusters.py \
    --cluster-file w<week>/<file> \
    --sample-file w<week>/<file> \
    --sample-file w<week>/<file> \
    --sample-file w<week>/<file> \
    --out-expanded w<week>/<file> \
    --out-platform "w<week>/<file>" \
    --stats-json w<week>/<file>

口径（ 2026-08-03 逐条确认，后续按此执行，勿再临场改）：

  【数据粒度】聚簇表一行 = 账号 × 一张资质图。一个账号可有多张图、分散在不同簇。
  这是本环节所有歧义的根源。

  【抽样账户分三类】去重后的抽样账户逐个到聚簇表里找：
    a) 找不到该账号            → 无资质图，从分母剔除
    b) 找得到但所有行 cluster_id 全为 -1 → 未成簇，留在分母，默认视为不违规
    c) 找得到且至少一行 cluster_id >= 0  → 成簇，留在分母
    分母 = b + c。判"未成簇"必须看**所有行**都是 -1，只要有一行成簇就算成簇。

  【命中簇】c 类账户所属的簇集合。

  【扩簇：以簇为单位整簇搬运】对命中簇，把**簇内所有行**写入（含抽样账户与同簇
  其他账户），让标注员看到完整的簇、能判断"这张图是否被多账号共用"。
    ⚠️ 边界：只写属于命中簇的行。若账号 B 因同簇被带出、但 B 名下另有一张图落在
    未被命中的簇 X，那张图**不写**——否则簇 X 会被顺带拖进标注文件，与本周抽样
    无关，标注员白标。（2026-08-03 教训：701 簇曾被这样撑成 1313。）

  【未成簇账户也写】b 类账户以 cluster_id=-1 写入。平台不会让人标，但保证
  "文件内抽样账户数 == 分母"，对账无需做减法。

  【剔除单账户簇】写完后检查每个有效簇：簇内只有 1 个 user_id 的整簇删除。
  这是同一账号自己的多张图相似所致，给不出"多账户共用"的信息。
    ⚠️ 陷阱：被删簇里可能坐着抽样账户，直接删会削掉分母。这类账户须以
    cluster_id=-1 写回，保证仍留在分母内。
    副作用（已确认接受）：「同账号重复传图」这个信号在标注文件里不再可见。

自检断言（任一不满足直接抛错退出，不产出残缺文件）：
  A1 输出文件内 cluster_id(>=0) 去重数 == 命中簇数（剔除单账户簇后）
  A2 输出文件内 is_sample_user=True 的账户数 == 有效抽样账户数（浓度分母）
  A3 有效抽样账户数 == 抽样去重总数 - 无资质图剔除数
  A4 扩簇账户与抽样账户无交集
  A5 输出文件行数 == 唯一 (user_id, qualification_url) 数（无重复行）
  A6 命中簇内的账户全部出现在文件里（防漏带同簇账号）
  A7 命中簇内的行全部出现在文件里（防漏图）
  A8 输出行序满足确定性契约（防 set 迭代乱序导致产物 md5 漂移）
  A9 过滤后不残留单账户簇
  A10 过滤未误伤全量表里本是多账户的真簇（那是真造假证据）
"""
import argparse
import csv
import json
import sys
from collections import defaultdict

PLATFORM_FIELDS = ["user_id", "qualification_url", "cluster_id",
                   "trade_first_name", "trade_second_name"]
EXPANDED_FIELDS = ["user_id", "trade_first_name", "trade_second_name",
                   "qualification_url", "cluster_id", "is_sample_user"]


def read_sample_uids(paths):
    """读多份抽样表，只取 user_id（列名在三份表间不统一，只有 user_id 稳定）"""
    uids = set()
    per_file = {}
    for p in paths:
        cnt = 0
        with open(p, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if "user_id" not in (reader.fieldnames or []):
                raise SystemExit(f"[FATAL] 抽样表缺少 user_id 列: {p} (实际列: {reader.fieldnames})")
            for row in reader:
                uid = (row.get("user_id") or "").strip()
                if uid:
                    uids.add(uid)
                    cnt += 1
        per_file[p] = cnt
    return uids, per_file


def read_cluster(path):
    """读全量聚簇结果，建三个索引"""
    uid_rows = defaultdict(list)     # user_id -> 该账户所有行
    uid_cids = defaultdict(set)      # user_id -> 所属有效簇集合
    cid_rows = defaultdict(list)     # cluster_id -> 簇内所有行
    total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        need = {"user_id", "qualification_url", "cluster_id"}
        missing = need - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"[FATAL] 聚簇表缺列 {missing}: {path}")
        for row in reader:
            total += 1
            uid = row["user_id"]
            try:
                cid = int(row["cluster_id"])
            except (TypeError, ValueError):
                raise SystemExit(f"[FATAL] cluster_id 非整数: {row.get('cluster_id')!r} (user_id={uid})")
            uid_rows[uid].append(row)
            if cid >= 0:
                uid_cids[uid].add(cid)
                cid_rows[cid].append(row)
    return uid_rows, uid_cids, cid_rows, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-file", required=True, help="全量聚簇结果 CSV")
    ap.add_argument("--sample-file", action="append", required=True,
                    help="抽样表 CSV，可重复传多份")
    ap.add_argument("--out-expanded", required=True,
                    help="输出 sample_expanded_clusters.csv（含 is_sample_user）")
    ap.add_argument("--out-platform", required=True,
                    help="输出标注平台上传文件（五列）")
    ap.add_argument("--keep-solo-clusters", action="store_true",
                    help="保留只有 1 个账户的簇（默认剔除：同账户自己多张图相似，无标注价值）")
    ap.add_argument("--stats-json", help="把统计量写成 JSON，便于下游填看板描述")
    args = ap.parse_args()

    sample_uids, per_file = read_sample_uids(args.sample_file)
    uid_rows, uid_cids, cid_rows, cluster_total_rows = read_cluster(args.cluster_file)

    # ---- 匹配 ----
    matched = {u for u in sample_uids if u in uid_rows}          # 有资质图 = 浓度分母
    no_qual_img = len(sample_uids) - len(matched)                 # 无资质图，剔除
    unmatched_cluster = sum(1 for u in matched if not uid_cids[u])  # 在表里但全 -1
    hit_clusters = set()
    for u in sorted(matched):
        hit_clusters |= uid_cids[u]

    # ---- 扩簇：只写命中簇内的行 ----
    out_rows = []
    seen = set()

    def add(row, is_sample):
        key = (row["user_id"], row["qualification_url"])
        if key in seen:
            return
        seen.add(key)
        out_rows.append({**row, "is_sample_user": str(is_sample)})

    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            add(row, row["user_id"] in matched)

    # 未成簇的抽样账户也写入（cluster_id=-1，平台不计入标注）
    for uid in sorted(matched):
        if not uid_cids[uid]:
            for row in uid_rows[uid]:
                add(row, True)

    # ---- 过滤单账户簇（2026-08-03 用户确认开启）----
    # 有效簇内若只有 1 个账户，说明是「同一账户自己的多张资质图高度相似」
    # （同份材料存了两版/拍了两次），不构成"多账户共用同一资质图"的造假证据，
    # 标注员看了也只能跳过。这类簇整簇剔除，让标注文件只留真正需要判断的内容。
    # 注意：剔除的是「簇」，被剔除簇里如果有抽样账户，该账户仍要留在文件里
    # （降级为 cluster_id=-1 段），否则浓度分母会被削掉。
    dropped_solo_clusters = set()
    dropped_rows = 0
    if not args.keep_solo_clusters:
        rows_by_cid = defaultdict(list)
        for r in out_rows:
            rows_by_cid[int(r["cluster_id"])].append(r)
        for cid, rs in rows_by_cid.items():
            if cid >= 0 and len({r["user_id"] for r in rs}) == 1:
                dropped_solo_clusters.add(cid)

        if dropped_solo_clusters:
            kept = []
            demote_uids = set()   # 被剔除簇里的抽样账户，需要保住分母
            for r in out_rows:
                cid = int(r["cluster_id"])
                if cid in dropped_solo_clusters:
                    dropped_rows += 1
                    if r["is_sample_user"] == "True":
                        demote_uids.add(r["user_id"])
                else:
                    kept.append(r)
            out_rows = kept
            # 抽样账户若因此完全从文件里消失，把它的行以 cluster_id=-1 写回
            still = {r["user_id"] for r in out_rows}
            seen = {(r["user_id"], r["qualification_url"]) for r in out_rows}
            for uid in sorted(demote_uids - still):
                for row in uid_rows[uid]:
                    key = (uid, row["qualification_url"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out_rows.append({**row, "cluster_id": "-1", "is_sample_user": "True"})
            # hit_clusters 同步收敛，A1 才能对得上
            hit_clusters = hit_clusters - dropped_solo_clusters

    # 统一按确定性契约排序：先有效簇（升序），再未成簇段；簇内按 user_id、url 升序。
    # 这样产物字节可复现，周与周之间能直接 diff。
    def _row_sort_key(r):
        c = int(r["cluster_id"])
        return (0 if c >= 0 else 1, c if c >= 0 else 0, r["user_id"], r["qualification_url"])
    out_rows.sort(key=_row_sort_key)
    seen = {(r["user_id"], r["qualification_url"]) for r in out_rows}

    sample_in_file = {r["user_id"] for r in out_rows if r["is_sample_user"] == "True"}
    expanded_in_file = {r["user_id"] for r in out_rows if r["is_sample_user"] == "False"}
    cids_in_file = {int(r["cluster_id"]) for r in out_rows if int(r["cluster_id"]) >= 0}

    # ---- 自检断言 ----
    errs = []
    if len(cids_in_file) != len(hit_clusters):
        errs.append(f"A1 文件内簇数 {len(cids_in_file)} != 命中簇数 {len(hit_clusters)}"
                    f"（多出的簇多半来自误带账户的其他图片）")
    if len(sample_in_file) != len(matched):
        errs.append(f"A2 文件内抽样账户数 {len(sample_in_file)} != 有效抽样账户数 {len(matched)}")
    if len(matched) != len(sample_uids) - no_qual_img:
        errs.append(f"A3 分母口径不自洽: matched={len(matched)}, "
                    f"sample={len(sample_uids)}, no_img={no_qual_img}")
    overlap = sample_in_file & expanded_in_file
    if overlap:
        errs.append(f"A4 扩簇账户与抽样账户有交集，{len(overlap)} 个，例: {list(overlap)[:3]}")
    if len(out_rows) != len(seen):
        errs.append(f"A5 存在重复行: rows={len(out_rows)}, unique={len(seen)}")

    # A6 命中簇内的账户必须全部出现在文件里（防"漏带同簇账户"——此时 A1/A2 都还是对的，看不出来）
    expect_uids = set()
    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            expect_uids.add(row["user_id"])
    got_uids = sample_in_file | expanded_in_file
    missing_uids = expect_uids - got_uids
    if missing_uids:
        errs.append(f"A6 命中簇内有 {len(missing_uids)} 个账户未写入文件"
                    f"（同簇账号被漏带，标注员将看不到复用证据），例: {list(missing_uids)[:3]}")

    # A7 命中簇内的行必须全部出现（防漏图）
    expect_keys = set()
    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            expect_keys.add((row["user_id"], row["qualification_url"]))
    missing_keys = expect_keys - seen
    if missing_keys:
        errs.append(f"A7 命中簇内有 {len(missing_keys)} 行未写入文件（漏图）")

    # A8 确定性自检：输出顺序必须只由数据决定，不受 Python 哈希随机化影响。
    # 做法是用「与写出顺序无关的规范化摘要」和「实际写出顺序的摘要」做对照——
    # 若存在未排序的 set 迭代，多次运行 out_rows 的顺序会漂移，
    # 而规范化摘要恒定，二者的关系（是否等于按 key 排序后的序列）就能暴露问题。
    actual_seq = [(r["user_id"], r["qualification_url"], r["cluster_id"]) for r in out_rows]
    # 期望顺序：先按 cluster_id 升序（-1 段最后），簇内按 user_id、url 升序
    def _sort_key(t):
        uid, url, cid = t
        c = int(cid)
        return (0 if c >= 0 else 1, c if c >= 0 else 0, uid, url)
    if actual_seq != sorted(actual_seq, key=_sort_key):
        errs.append("A8 输出行序不满足确定性排序契约"
                    "（可能存在未排序的 set 迭代，会导致每次运行产物 md5 漂移、无法 diff 核对）")

    # A9 过滤后文件内不应再有单账户簇（除非显式 --keep-solo-clusters）
    if not args.keep_solo_clusters:
        cl_uids = defaultdict(set)
        for r in out_rows:
            c = int(r["cluster_id"])
            if c >= 0:
                cl_uids[c].add(r["user_id"])
        residual = [c for c, us in cl_uids.items() if len(us) == 1]
        if residual:
            errs.append(f"A9 过滤后仍存在 {len(residual)} 个单账户簇，例: {residual[:3]}")

    # A10 过滤不得误伤真正的多账户簇：被剔除的簇在全量聚簇表里也必须只有 1 个账户
    if dropped_solo_clusters:
        misfired = []
        for cid in dropped_solo_clusters:
            if len({r["user_id"] for r in cid_rows[cid]}) > 1:
                misfired.append(cid)
        if misfired:
            errs.append(f"A10 误剔除了 {len(misfired)} 个全量表里本是多账户的簇，"
                        f"例: {misfired[:3]}（这些是真造假证据，不能删）")

    if errs:
        print("=" * 60, file=sys.stderr)
        print("[FATAL] 自检未通过，未写出任何文件：", file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)

    # ---- 写文件 ----
    with open(args.out_expanded, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPANDED_FIELDS)
        w.writeheader()
        for r in out_rows:
            w.writerow({k: r.get(k, "") for k in EXPANDED_FIELDS})

    with open(args.out_platform, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PLATFORM_FIELDS)
        w.writeheader()
        for r in out_rows:
            w.writerow({k: r.get(k, "") for k in PLATFORM_FIELDS})

    stats = {
        "sample_total": len(sample_uids),
        "sample_per_file": per_file,
        "no_qual_img_excluded": no_qual_img,
        "valid_sample_users": len(matched),        # 浓度分母
        "unmatched_cluster": unmatched_cluster,
        "hit_clusters": len(hit_clusters),
        "expanded_users": len(expanded_in_file),
        "annotate_users_total": len(sample_in_file) + len(expanded_in_file),
        "annotate_rows_total": len(out_rows),
        "dropped_solo_clusters": len(dropped_solo_clusters),
        "dropped_solo_rows": dropped_rows,
        "cluster_source_rows": cluster_total_rows,
    }

    # 看板 match_expand 描述（第一句固定为"共匹配上 X 个抽样账户（命中 Y 个簇）"）
    stats["dashboard_message"] = (
        f"共匹配上 {stats['valid_sample_users']} 个抽样账户（命中 {stats['hit_clusters']} 个簇）\n"
        f"抽样总数 {stats['sample_total']}，因无资质图剔除 {stats['no_qual_img_excluded']}，"
        f"有效抽样账户数 {stats['valid_sample_users']}（浓度分母），"
        f"未匹配到簇 {stats['unmatched_cluster']}，带出同簇账户 {stats['expanded_users']}，"
        f"待标注合计 {stats['annotate_users_total']} 账户（{stats['annotate_rows_total']} 行）"
    )

    if args.stats_json:
        with open(args.stats_json, "w") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    print("自检通过 ✓  (A1~A10：簇数/分母/口径/无交集/无重复/同簇全带/无漏图/确定性/无单账户簇/过滤无误伤)")
    print()
    print(f"抽样总数              : {stats['sample_total']}")
    print(f"无资质图剔除          : {stats['no_qual_img_excluded']}")
    print(f"有效抽样账户（分母）  : {stats['valid_sample_users']}")
    print(f"未匹配到簇            : {stats['unmatched_cluster']}")
    print(f"命中簇数              : {stats['hit_clusters']}")
    print(f"扩簇带出账户          : {stats['expanded_users']}")
    print(f"待标注账户合计        : {stats['annotate_users_total']}")
    print(f"待标注行数            : {stats['annotate_rows_total']}")
    print(f"剔除单账户簇          : {stats['dropped_solo_clusters']} 个（{stats['dropped_solo_rows']} 行）")
    print()
    print(f"→ {args.out_expanded}")
    print(f"→ {args.out_platform}")
    if args.stats_json:
        print(f"→ {args.stats_json}")


if __name__ == "__main__":
    main()
