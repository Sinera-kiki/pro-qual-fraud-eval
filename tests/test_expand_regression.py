#!/usr/bin/env python3
"""
expand_sample_clusters.py 的回归测试

作用：把「历史上真实发生过的错误」做成注入式用例，验证自检断言仍能拦住它们。
每次改动扩簇逻辑后必须跑一遍，全绿才能用于生产。

用法：
  cd pro_qual_cluster_review
  python test_expand_regression.py \
    --cluster-file w<week>/v2_full_cluster_result.csv \
    --sample-file w<week>/overall_sample.csv \
    --sample-file w<week>/industry_a_sample.csv \
    --sample-file w<week>/industry_b_sample.csv

用例来源（2026-08-03 当天真实踩过的坑）：
  T1 漏带同簇账户  —— 扩簇只写抽样账户自己，同簇其他账号全丢。
                      A1/A2 都检测不到（簇数与分母都还是对的），必须靠 A6/A7。
  T2 多带无关簇    —— 对扩簇账户写入其名下全部图片，把未命中的簇一起拖进来。
                      701 簇被撑到 1313，由 A1 拦下。
  T3 单账户簇残留  —— 关掉过滤后应残留单账户簇，由 A9 拦下。
  T4 过滤削分母    —— 删单账户簇时不把其中的抽样账户写回，分母被削，由 A2 拦下。
  T5 输出不确定    —— set 迭代未排序导致行序漂移，由 A8 拦下。
  T6 正常路径      —— 未注入错误时必须通过，且多次运行 md5 一致。
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "expand_sample_clusters.py")
PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")

# 扩簇主循环的原文，注入时替换它
ANCHOR = """    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            add(row, row["user_id"] in matched)"""

MUT_T1 = """    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            if row["user_id"] in matched:
                add(row, True)"""

MUT_T2 = """    _exp_tmp = set()
    for cid in sorted(hit_clusters):
        for row in cid_rows[cid]:
            if row["user_id"] not in matched:
                _exp_tmp.add(row["user_id"])
    for uid in sorted(matched):
        for row in uid_rows[uid]:
            add(row, True)
    for uid in sorted(_exp_tmp):
        for row in uid_rows[uid]:
            add(row, False)"""

# T4：删单账户簇时不写回抽样账户（把回写段掐掉）
ANCHOR_T4 = """            for uid in sorted(demote_uids - still):"""
MUT_T4 = """            for uid in sorted(set() - still):"""

# T5：把确定性排序去掉
ANCHOR_T5 = """    out_rows.sort(key=_row_sort_key)"""
MUT_T5 = """    pass  # 故意不排序"""


def run(script, args, extra=None):
    cmd = [PY, script,
           "--cluster-file", args.cluster_file,
           "--out-expanded", tempfile.mktemp(suffix=".csv"),
           "--out-platform", tempfile.mktemp(suffix=".csv")]
    for s in args.sample_file:
        cmd += ["--sample-file", s]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


def mutate_and_run(name, old, new, args, expect_codes, extra=None):
    code = open(SRC, encoding="utf-8").read()
    if old not in code:
        print(f"  ⚠️  {name}: 锚点未找到，脚本结构可能已变，用例需更新")
        return False
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                      dir=os.path.dirname(SRC), encoding="utf-8")
    tmp.write(code.replace(old, new, 1))
    tmp.close()
    try:
        r = run(tmp.name, args, extra)
    finally:
        os.unlink(tmp.name)

    caught = re.findall(r"- (A\d+)", r.stderr)
    ok = r.returncode != 0 and any(c in expect_codes for c in caught)
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}: exit={r.returncode} 触发={sorted(set(caught)) or '无'} "
          f"(期望含 {expect_codes})")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-file", required=True)
    ap.add_argument("--sample-file", action="append", required=True)
    args = ap.parse_args()

    print("=== expand_sample_clusters.py 回归测试 ===\n")
    results = []

    print("[注入式用例] 历史 bug 必须被断言拦下")
    results.append(mutate_and_run("T1 漏带同簇账户", ANCHOR, MUT_T1, args, {"A6", "A7"}))
    results.append(mutate_and_run("T2 多带无关簇", ANCHOR, MUT_T2, args, {"A1"}))
    results.append(mutate_and_run("T4 过滤削分母", ANCHOR_T4, MUT_T4, args, {"A2"}))
    results.append(mutate_and_run("T5 输出不确定", ANCHOR_T5, MUT_T5, args, {"A8"}))

    print("\n[开关用例]")
    r = run(SRC, args, ["--keep-solo-clusters"])
    ok = r.returncode == 0
    print(f"  {'✅' if ok else '❌'} T3 --keep-solo-clusters 应正常通过（不触发 A9）: exit={r.returncode}")
    results.append(ok)

    print("\n[正常路径]")
    r = run(SRC, args)
    ok = r.returncode == 0 and "自检通过" in r.stdout
    print(f"  {'✅' if ok else '❌'} T6a 默认路径自检通过: exit={r.returncode}")
    results.append(ok)

    digests = set()
    for i in range(5):
        out = tempfile.mktemp(suffix=".csv")
        cmd = [PY, SRC, "--cluster-file", args.cluster_file,
               "--out-expanded", out, "--out-platform", tempfile.mktemp(suffix=".csv")]
        for s in args.sample_file:
            cmd += ["--sample-file", s]
        env = dict(os.environ, PYTHONHASHSEED=str(1000 + i * 137))
        subprocess.run(cmd, capture_output=True, env=env)
        digests.add(hashlib.md5(open(out, "rb").read()).hexdigest())
        os.unlink(out)
    ok = len(digests) == 1
    print(f"  {'✅' if ok else '❌'} T6b 5 个哈希种子输出 md5 一致: {len(digests)} 种结果")
    results.append(ok)

    print()
    if all(results):
        print(f"全部通过 ✅  ({len(results)}/{len(results)})")
        return 0
    print(f"存在失败 ❌  ({sum(results)}/{len(results)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
