#!/usr/bin/env python3
"""
按行业统计「新户跟历史聚上簇」比例，多日汇总。

口径（与标注任务一致）：
- 只保留 cluster_user_count >= 2 的簇（排除单账号自相似簇，这些不进标注）
- 目标日新户 = is_target_user=True 且落在上述有效簇内
- 簇打标：簇内含历史账号 → 混合簇；否则纯当天簇
- 账号归类（按 user_id 聚合其所有簇）：
    仅当天簇 / 跟历史聚上簇（= 仅混合簇 + 两者都有）
- 行业比例 = 跟历史聚上簇账号数 / 该行业待标注新户数
"""
import os, glob
import pandas as pd

MIN_CLUSTER_USERS = 2


def analyze_one(path):
    df = pd.read_csv(path, usecols=['user_id', 'trade_first_name',
                                    'is_target_user', 'cluster_id'])
    df['is_target_user'] = df['is_target_user'].astype(str).str.lower().isin(['true', '1'])
    c = df[df['cluster_id'] >= 0]
    if c.empty:
        return None, None

    # 单账号自相似簇过滤
    cu = c.groupby('cluster_id')['user_id'].nunique()
    valid_ids = cu[cu >= MIN_CLUSTER_USERS].index
    c = c[c['cluster_id'].isin(valid_ids)]
    if c.empty:
        return None, None

    # 每个簇是否含历史账号
    has_hist = c.groupby('cluster_id')['is_target_user'].apply(lambda s: (~s).any())

    tgt = c[c['is_target_user']].copy()
    if tgt.empty:
        return None, None
    tgt['cluster_has_history'] = tgt['cluster_id'].map(has_hist)

    agg = tgt.groupby('user_id').agg(
        industry=('trade_first_name', 'first'),
        hist_flags=('cluster_has_history', list),
    )
    agg['in_mixed'] = agg['hist_flags'].apply(lambda xs: True in xs)
    agg['cat'] = 'pure_only'
    agg.loc[agg['in_mixed'], 'cat'] = 'with_hist'

    res = agg.groupby(['industry', 'cat']).size().unstack(fill_value=0)
    for col in ['pure_only', 'with_hist']:
        if col not in res.columns:
            res[col] = 0
    res = res[['pure_only', 'with_hist']]
    res['total'] = res['pure_only'] + res['with_hist']

    meta = {
        'valid_clusters': int(len(valid_ids)),
        'dropped_single': int((cu < MIN_CLUSTER_USERS).sum()),
        'task_accounts': int(c['user_id'].nunique()),
        'task_rows': int(len(c)),
    }
    return res, meta


def main():
    files = {}
    if os.path.exists('cmp_20260803/B_cluster.csv'):
        files['2026-08-03'] = 'cmp_20260803/B_cluster.csv'
    for d in sorted(glob.glob('multi_day/*/cluster.csv')):
        ds = d.split('/')[1]
        files[f'{ds[:4]}-{ds[4:6]}-{ds[6:]}'] = d

    per_day, metas = {}, {}
    print('【逐日概况（已过滤单账号簇）】')
    for label in sorted(files):
        r, meta = analyze_one(files[label])
        if r is None:
            print(f'  {label}: 无有效数据')
            continue
        per_day[label] = r
        metas[label] = meta
        tot, wh = r['total'].sum(), r['with_hist'].sum()
        print(f'  {label}: 待标注新户 {tot:>4} | 跟历史 {wh:>4} ({wh/tot*100:5.1f}%) '
              f'| 有效簇 {meta["valid_clusters"]:>4} | 剔除单账号簇 {meta["dropped_single"]:>3} '
              f'| 待标注账号 {meta["task_accounts"]:>4}')

    if not per_day:
        return

    all_ind = sorted(set().union(*[set(r.index) for r in per_day.values()]))
    rows = []
    for ind in all_ind:
        pure = sum(int(r.loc[ind, 'pure_only']) for r in per_day.values() if ind in r.index)
        wh = sum(int(r.loc[ind, 'with_hist']) for r in per_day.values() if ind in r.index)
        tot = pure + wh
        daily = [r.loc[ind, 'with_hist'] / r.loc[ind, 'total'] * 100
                 for r in per_day.values() if ind in r.index and r.loc[ind, 'total'] > 0]
        mean_pct = sum(daily) / len(daily) if daily else 0.0
        spread = (max(daily) - min(daily)) if len(daily) > 1 else 0.0
        rows.append({
            'industry': ind,
            'pure_only': pure,
            'with_hist': wh,
            'total': tot,
            'pooled_pct': round(wh / tot * 100, 1) if tot else 0.0,
            'mean_daily_pct': round(mean_pct, 1),
            'min_pct': round(min(daily), 1) if daily else 0.0,
            'max_pct': round(max(daily), 1) if daily else 0.0,
            'spread': round(spread, 1),
            'days': len(daily),
            'avg_per_day': round(tot / len(daily), 1) if daily else 0.0,
        })

    out = pd.DataFrame(rows).sort_values('mean_daily_pct', ascending=False)
    out.to_csv('multi_day/industry_summary.csv', index=False, encoding='utf-8-sig')

    g_pure = sum(r['pure_only'].sum() for r in per_day.values())
    g_wh = sum(r['with_hist'].sum() for r in per_day.values())
    g_tot = g_pure + g_wh
    print()
    print(f'【{len(per_day)} 天合计】待标注新户 {g_tot} | 跟历史 {g_wh} ({g_wh/g_tot*100:.1f}%) '
          f'| 仅当天 {g_pure} ({g_pure/g_tot*100:.1f}%)')
    print()
    print('【按行业（逐日比例均值降序）】')
    print(out.to_string(index=False))

    detail = []
    for label, r in sorted(per_day.items()):
        for ind in r.index:
            t = int(r.loc[ind, 'total'])
            detail.append({
                'date': label, 'industry': ind,
                'pure_only': int(r.loc[ind, 'pure_only']),
                'with_hist': int(r.loc[ind, 'with_hist']),
                'total': t,
                'pct': round(r.loc[ind, 'with_hist'] / t * 100, 1) if t else 0.0,
            })
    pd.DataFrame(detail).to_csv('multi_day/industry_daily.csv',
                                index=False, encoding='utf-8-sig')
    print()
    print('已写出 multi_day/industry_summary.csv 与 industry_daily.csv')


if __name__ == '__main__':
    main()
