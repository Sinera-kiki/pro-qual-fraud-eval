#!/usr/bin/env python3
"""生成各行业「跟历史聚上簇」比例柱状图（5天均值，按比例降序）"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import pandas as pd
import numpy as np

# 中文字体
for fp in ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
           '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
           '/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc',
           '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc']:
    try:
        font_manager.fontManager.addfont(fp)
        prop = font_manager.FontProperties(fname=fp)
        plt.rcParams['font.family'] = prop.get_name()
        print('字体:', prop.get_name())
        break
    except Exception:
        continue
plt.rcParams['axes.unicode_minus'] = False

df = pd.read_csv('multi_day/industry_summary.csv')

# 只画日均 >=10 个账号的行业，其余合并为「其他小行业」
MAIN_THRESHOLD = 10
main = df[df['avg_per_day'] >= MAIN_THRESHOLD].copy()
rest = df[df['avg_per_day'] < MAIN_THRESHOLD]

if len(rest):
    r_pure, r_wh = rest['pure_only'].sum(), rest['with_hist'].sum()
    r_tot = r_pure + r_wh
    main = pd.concat([main, pd.DataFrame([{
        'industry': '其他小行业', 'pure_only': r_pure, 'with_hist': r_wh,
        'total': r_tot,
        'pooled_pct': round(r_wh / r_tot * 100, 1) if r_tot else 0,
        'mean_daily_pct': round(r_wh / r_tot * 100, 1) if r_tot else 0,
        'min_pct': 0, 'max_pct': 0, 'spread': 0, 'days': 5,
        'avg_per_day': round(r_tot / 5, 1),
    }])], ignore_index=True)

main = main.sort_values('mean_daily_pct', ascending=True)  # 横向图从下往上

fig, ax = plt.subplots(figsize=(11, 7.5))
y = np.arange(len(main))
vals = main['mean_daily_pct'].values

# 颜色分档
colors = []
for v in vals:
    if v >= 80:
        colors.append('#c0392b')      # 高：历史挂靠主导
    elif v >= 50:
        colors.append('#e67e22')      # 中
    else:
        colors.append('#2980b9')      # 低：当天互撞主导

bars = ax.barh(y, vals, color=colors, height=0.62, zorder=3)

# 误差线（min~max 区间）
is_other = main['industry'].values == '其他小行业'
for i, (lo, hi, v, skip) in enumerate(zip(main['min_pct'], main['max_pct'], vals, is_other)):
    if skip or hi <= 0:
        continue
    ax.plot([lo, hi], [i, i], color='#555', lw=1.1, zorder=4, alpha=0.75)
    for x in (lo, hi):
        ax.plot([x, x], [i - 0.13, i + 0.13], color='#555', lw=1.1, zorder=4, alpha=0.75)

ax.set_yticks(y)
ax.set_yticklabels(main['industry'], fontsize=11)
ax.set_xlabel('跟历史聚上簇的新户占比（5 天均值，%）', fontsize=11.5)
ax.set_title('各行业新户「资质图与历史账号重复」比例\n2026-07-31 ~ 08-04 共 5 天，已过滤单账号自相似簇',
             fontsize=13.5, pad=14)
ax.set_xlim(0, 125)
ax.grid(axis='x', alpha=0.28, zorder=0)
ax.set_axisbelow(True)

# 数值 + 日均样本量
for i, (v, n) in enumerate(zip(vals, main['avg_per_day'])):
    ax.text(v + 2.0, i, f'{v:.1f}%', va='center', fontsize=10.5, fontweight='bold')
    ax.text(123, i, f'{n:.0f}/天', va='center', ha='right',
            fontsize=8.8, color='#777')

# 整体均值参考线
overall = 76.2
ax.axvline(overall, color='#333', ls='--', lw=1.2, alpha=0.65, zorder=2)
ax.text(overall + 0.8, len(main) - 0.35, f'整体 {overall}%',
        fontsize=9.5, color='#333', alpha=0.85)

from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(facecolor='#c0392b', label='≥80%  历史挂靠主导'),
    Patch(facecolor='#e67e22', label='50~80% 混合'),
    Patch(facecolor='#2980b9', label='<50%  当天互撞为主'),
], loc='lower right', fontsize=9.5, framealpha=0.92)

plt.figtext(0.5, -0.012,
            '横线为 5 天内的最低~最高区间；右侧灰字为该行业日均待标注新户数',
            fontsize=9, color='#777', ha='center')

plt.tight_layout()
plt.savefig('multi_day/industry_chart.png', dpi=155, bbox_inches='tight',
            facecolor='white')
print('已保存 multi_day/industry_chart.png')
print(main[['industry', 'mean_daily_pct', 'avg_per_day']].to_string(index=False))
