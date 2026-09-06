#!/usr/bin/env python3
"""
周度留存表回填脚本（固化版，2026-08-18）。

背景：hi sheets:read 对单次读取行数有上限（实测 A1:H20 可读、A1:H40 报
[1001004] 参数错误），本脚本内部分段读取（每段 20 行）拼全表，规避该限制。

功能：
  1. 分段读取「数据情况」工作表全量，判断本周区块（按时间周期匹配）是否已存在。
  2. 不存在 → 在末尾追加「表头行 + 三行数据（大盘整体/定向加抽行业）」。
  3. 已存在 → 只改与传入值有差异的单元格（用户可能手工填过，不覆盖样式）。
  4. 写入后回读校验关键数字是否落地。

列序：时间周期 / 评估日期 / 行业 / 评估量级 / 违规量级 / 违规浓度 / 数据留存 / 备注
约定：违规浓度写小数（如 0.0571，数字格式）；数据留存与备注只填区块第一行。

用法（由周度轮询任务调用，也可手动）：
  python backfill_retention.py \
    --period "8.10-8.16" --eval-date 8.18 \
    --overall 1278 73 0.0571 \
    --industry-a 242 184 0.7603 \
    --industry-b 684 97 0.1418 \
    --data-file annotated_w33.csv \
    --note "分母剔除聚簇表中未匹配的账户" \
    [--dry-run]

注意：本脚本通过 subprocess 调 `hi` CLI 完成读写（sheets:read / sheets:update），
不要用 python 直连 REDoc 接口——鉴权与协议细节都封装在 CLI 里。
"""
import argparse
import json
import math
import subprocess
import sys

SHORTCUT_ID = "<REDOC_SHORTCUT_ID>"
SHEET = "数据情况"
READ_CHUNK = 20          # 单次 sheets:read 行数上限（实测 20 安全，40 报参数错误）
COLS = 8                 # A..H

HEADER_ROW = ["时间周期（指聚簇周期）", "评估日期", "行业", "评估量级",
              "违规量级", "违规浓度", "数据留存", "备注"]


def run_hi(args, retries=3):
    """调 hi CLI，返回 stdout；sheets:read 偶发 504/参数错，重试。"""
    last_err = None
    for i in range(retries):
        r = subprocess.run(["hi"] + args, capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        if out.startswith("{") and '"error"' not in out[:200] and '"error"' not in out:
            return out
        # 504 或参数错误 → 重试；先小睡
        last_err = out or r.stderr
        subprocess.run(["sleep", "2"])
    raise RuntimeError(f"hi CLI 失败（重试{retries}次）: {last_err[:300]}")


def read_all():
    """分段读取全表，返回 list[list]。空尾行被裁掉。"""
    rows, start, seen_empty = [], 1, 0
    while True:
        rng = f"{SHEET}!A{start}:H{start + READ_CHUNK - 1}"
        out = run_hi(["sheets:read", "--shortcut-id", SHORTCUT_ID, "--range", rng])
        data = json.loads(out)["values"]
        chunk_nonempty = [r for r in data if any(c not in (None, "") for c in r)]
        if not chunk_nonempty and seen_empty >= 1:
            break
        if not chunk_nonempty:
            seen_empty += 1
            start += READ_CHUNK
            continue
        seen_empty = 0
        # 补齐列数
        for r in chunk_nonempty:
            r = list(r) + [""] * (COLS - len(r))
            rows.append(r[:COLS])
        start += READ_CHUNK
    return rows


def find_block(rows, period):
    """返回本周区块首行索引（1-based），不存在返回 None。
    判据：该行 A 列 == period 且 C 列 == '大盘整体'。"""
    for i, r in enumerate(rows, start=1):
        if str(r[0]).strip() == period and str(r[2]).strip() == "大盘整体":
            return i
    return None


def num_eq(a, b):
    """数值宽松相等（表里可能存 0.7603000001 这类浮点）。"""
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def update_range(a1, values):
    rng = f"{SHEET}!A{a1}:H{a1 + len(values) - 1}"
    payload = json.dumps(values, ensure_ascii=False)
    out = run_hi(["sheets:update", "--shortcut-id", SHORTCUT_ID,
                  "--range", rng, "--values", payload])
    return json.loads(out).get("updated") is True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, help="时间周期，如 8.10-8.16")
    ap.add_argument("--eval-date", required=True, help="评估日期，如 8.18（数字）")
    ap.add_argument("--overall", nargs=3, required=True,
                    metavar=("分母", "分子", "浓度"), help="大盘整体")
    ap.add_argument("--industry_a", nargs=3, required=True, metavar=("分母", "分子", "浓度"))
    ap.add_argument("--industry_b", nargs=3, required=True, metavar=("分母", "分子", "浓度"))
    ap.add_argument("--data-file", required=True, help="数据留存：标注结果文件名")
    ap.add_argument("--note", default="分母剔除聚簇表中未匹配的账户")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    def n(x):
        return float(x) if "." in x else int(x)

    block_rows = [
        [args.period, n(args.eval_date), "大盘整体", n(args.overall[0]),
         n(args.overall[1]), n(args.overall[2]), args.data_file, args.note],
        [args.period, n(args.eval_date), "定向加抽行业", n(args.industry_a[0]),
         n(args.industry_a[1]), n(args.industry_a[2]), "", ""],
        [args.period, n(args.eval_date), "定向加抽行业", n(args.industry_b[0]),
         n(args.industry_b[1]), n(args.industry_b[2]), "", ""],
    ]

    rows = read_all()
    print(f"读全表 {len(rows)} 行")
    blk = find_block(rows, args.period)

    if blk is None:
        # 追加新区块：表头 + 3 行
        append_start = len(rows) + 1
        payload = [HEADER_ROW] + block_rows
        print(f"本周区块不存在，追加至第 {append_start}~{append_start+3} 行")
        if not args.dry_run:
            ok = update_range(append_start, payload)
            print("写入:", "成功" if ok else "失败")
            if not ok:
                sys.exit(1)
    else:
        # 已存在：逐格比对，只改差异单元格
        diffs = []
        for ri, new_row in enumerate(block_rows):
            cur = rows[blk - 1 + ri]
            for ci in range(COLS):
                old, new = cur[ci], new_row[ci]
                if new == "" and old == "":
                    continue
                if num_eq(old, new):
                    continue
                diffs.append((blk + ri, "ABCDEFGH"[ci], old, new))
        if not diffs:
            print("本周区块已存在且数值一致，无需改动")
            return
        print(f"本周区块已存在（第 {blk} 行起），差异单元格 {len(diffs)} 个：")
        for r, c, old, new in diffs:
            print(f"  {c}{r}: {old!r} -> {new!r}")
        if not args.dry_run:
            # 整块覆盖重写（2026-08-19 修：逐格写会因 None 占位导致行错位/丢格）
            update_range(blk, block_rows)
            print("差异已整块重写")

    if args.dry_run:
        print("[dry-run] 未写入")
        return

    # 回读校验：浓度三个数
    rows2 = read_all()
    blk2 = find_block(rows2, args.period)
    if blk2 is None:
        print("⚠️ 回读未找到本周区块，请人工核对")
        sys.exit(1)
    ok = True
    for ri, grp in enumerate([args.overall, args.industry_a, args.industry_b]):
        got = rows2[blk2 - 1 + ri][5]
        if not num_eq(got, grp[2]):
            print(f"⚠️ 回读校验失败 第{blk2+ri}行 浓度 {got} != {grp[2]}")
            ok = False
    print("回读校验:", "通过" if ok else "有失败项")
    if not ok:
        sys.exit(1)
    print("提醒：CLI 写入不带样式，需在网页端用格式刷把新区块刷成历史格式（仅新增区块时）")


if __name__ == "__main__":
    main()
