#!/usr/bin/env python3
"""
抽样量计算 + 上周自然周时间范围计算
移植自《商家账号资质造假浓度评估自动化工作流方案》文档中的 JS 公式。

用法：
  # 计算上周自然周范围（周一~周日）与 dtm
  python calc_sample_size.py --week

  # 计算样本量: N=大N, p=上周浓度(小数), 置信度默认0.99
  # e 规则: p < 10% -> e=2%; p >= 10% -> e=5% (可用 --e 覆盖)
  python calc_sample_size.py --n 14320 --p 0.0723
  python calc_sample_size.py --n 361 --p 0.5170
"""
import argparse
import json
import math
from datetime import date, timedelta


def normal_cdf_inv(p: float) -> float:
    """标准正态 CDF 反函数（Acklam 近似，与文档 JS 版一致）"""
    if p <= 0:
        return float("-inf")
    if p >= 1:
        return float("inf")
    a = [2.515517, 0.802853, 0.010328]
    b = [1.432788, 0.189269, 0.001308]
    t = math.sqrt(-2 * math.log(p)) if p < 0.5 else math.sqrt(-2 * math.log(1 - p))
    num = a[0] + t * (a[1] + t * a[2])
    den = 1 + t * (b[0] + t * (b[1] + t * b[2]))
    z = t - num / den
    return -z if p < 0.5 else z


def conf_to_z(conf: float) -> float:
    return normal_cdf_inv(1 - (1 - conf) / 2)


def calc_n(z: float, p: float, e: float, big_n: float | None) -> float:
    n0 = z * z * p * (1 - p) / (e * e)
    if not big_n:
        return n0
    return n0 * big_n / (n0 + big_n - 1)  # 有限总体修正


def default_e(p: float) -> float:
    """误差范围规则（2026-07-31 用户确认）：浓度 30% 以下 e=2%，30% 及以上 e=5%"""
    return 0.02 if p < 0.30 else 0.05


# SPECIAL_INDUSTRY_A固定全量取，不走抽样公式（口径已确认）。
# 原因：定向加抽行业周入驻量只有 30 上下，公式在 P=0（上周 0 违规）时会算出样本量 0，
# 而按上一期 P 回退又几乎等于全量，来回折腾没有意义。直接全量，成本极低且无失真。
FULL_TAKE_INDUSTRIES = {"SPECIAL_INDUSTRY_A"}


def is_full_take(industry: str) -> bool:
    return industry in FULL_TAKE_INDUSTRIES


def last_week_range(today: date | None = None) -> dict:
    """上一个完整自然周：周一 ~ 周日；dtm = 上周日 yyyymmdd"""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return {
        "start": last_monday.isoformat(),
        "end": last_sunday.isoformat(),
        "dtm": last_sunday.strftime("%Y%m%d"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", action="store_true", help="输出上周自然周范围")
    ap.add_argument("--n", type=float, help="大 N（总体量）")
    ap.add_argument("--p", type=float, help="上周浓度，小数形式如 0.0723")
    ap.add_argument("--conf", type=float, default=0.99, help="置信度，默认 0.99")
    ap.add_argument("--e", type=float, help="误差范围，缺省按文档规则自动定")
    ap.add_argument("--industry", help="行业名；命中全量取清单（如 SPECIAL_INDUSTRY_A）时直接返回大 N")
    args = ap.parse_args()

    if args.week:
        print(json.dumps(last_week_range(), ensure_ascii=False))
    elif args.industry and is_full_take(args.industry):
        if args.n is None:
            ap.error("全量取行业仍需 --n 传入大 N")
        print(json.dumps({
            "industry": args.industry, "big_N": args.n, "mode": "full_take",
            "sample_size": int(args.n),
            "note": "该行业固定全量取，不走抽样公式（2026-08-03 确认）",
        }, ensure_ascii=False))
    else:
        if args.n is None or args.p is None:
            ap.error("需要 --n 与 --p")
        e = args.e if args.e is not None else default_e(args.p)
        z = conf_to_z(args.conf)
        n = calc_n(z, args.p, e, args.n)
        print(json.dumps({
            "big_N": args.n, "p": args.p, "e": e, "conf": args.conf,
            "z": round(z, 6), "sample_size": math.ceil(n),
        }, ensure_ascii=False))
