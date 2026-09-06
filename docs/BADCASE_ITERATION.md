# 周度实锤审核对比（badcase 漏拦截定位）

每周对标注为「实锤造假」的账号，做一轮**实锤图 vs 机审送审图**的对照分析，定位这些本应被拦住的账号到底是在哪一环漏掉的，据此迭代算法与策略。

## 为什么需要这一步

聚簇 + 标注 + 浓度评估解决的是"看清造假水位"，高危模板入库解决的是"把实锤模板沉淀为拦截素材"。但还缺最后一环：一个账号被人工实锤造假，当初机审为什么放行了？不查清漏在哪，算法和策略就没法精准迭代。

## 固定脚本

`scripts/weekly_hammer_audit_compare.py` —— 一条命令跑完全链路：

```bash
python scripts/weekly_hammer_audit_compare.py --csv <本周标注CSV> --week wXX
```

链路步骤：

1. **实锤筛选**：标注 CSV 里筛 `remark_first = '实锤造假'` 的账号。
2. **过滤号店**：剔除 `apply_source = 'SELLER_PASS'` 的号店账号（号店属于交易部业务，不在本流程范围）。
3. **逐 uid 取因子**：调风控命中日志接口，取 `proAccountQualificationSimResList` 相似检索因子 + 处置结果。
4. **实锤图 vs 送审图分类**：比对实锤账号的资质图与机审送审图（`query_img_url`）的交集，分三类。
5. **生成对照产物**：输出对照 HTML（可视化左右对照）+ 汇总 JSON。

## 定时任务

每周三 18:00 自动执行（标注结果一般周三定稿，预留下午缓冲）。流程：

1. 找本周 `pro_qual_cluster_review/wXX/` 下的标注 CSV（找不到则停下汇报，不瞎跑）；
2. 跑固定脚本；
3. HTML 传 CDN；
4. 汇报三类分布 + 链接 + 各类账号清单 + 疑点。

## 环境依赖说明

脚本查询风控命中日志时依赖 `fetch_factor` 模块（内网 risk-hitlog-factor skill）；查入驻表 apply_source 时依赖内网 Dataverse SQL CLI（`dp dataverse sql direct-sr`）。本仓已提供可读桩 [scripts/fetch_factor_stub.py](scripts/fetch_factor_stub.py)，文档化了 `fetch_factor` 接口契约（load_cookie / parse_time / call 的输入输出与 records 结构）。

- 生产环境：真实 `fetch_factor` 存在时自动优先使用；
- 本地/外部阅读：缺依赖时自动回退到桩实现，脚本仍能以「因子无值」路径正常跑通，便于阅读逻辑；
- SQL 部分（`run_sql` 函数）通过 `dp dataverse sql direct-sr` CLI 执行，代码内有注释说明，无需桩实现。

## 分类口径（定位规则）

三类分类，各对应一个可落地的整改方向：

| 分类 | 判定 | 含义 | 对应问题 |
| --- | --- | --- | --- |
| **实锤图 = 送审图** | 因子有值，机审见过这张图且命中了，但最终放行 | 图进了机审、也被判中，但没被拦住 | 大模型工作流问题 |
| **实锤图 ≠ 送审图** | 机审审过账号，但送审图里没有实锤那张图 | 账号过审但关键图没进机审 | qcode 拆分 / 换图类 |
| **因子无值** | 相似检索没召回任何结果 | 图压根没被相似检索命中 | 算法向量检索问题 |

判定实现（脚本内）：

```python
fake_urls = 实锤账号的资质图 URL 集合
q_urls    = 机审命中因子(query_img_url)的送审图 URL 集合

if not q_urls:      match = 'nofactor'   # 因子无值 → 向量检索问题
elif fake_urls & q_urls: match = 'same'  # 实锤图 = 送审图 → 大模型工作流问题
else:               match = 'diff'       # 实锤图 ≠ 送审图 → 图未进机审
```

## 结果如何反哺闭环

| 分类结果 | 迭代动作 |
| --- | --- |
| 实锤图 = 送审图（工作流问题） | 排查大模型判定/放行链路，修正放行阈值或策略组合 |
| 实锤图 ≠ 送审图（图未进机审） | 修 qcode 拆分 / 换图场景，确保实锤图能送进机审 |
| 因子无值（向量检索问题） | 迭代聚簇/向量检索（top-k、相似阈值、簇间补边），让这张图能被召回 |

这一步把单向的"评估-拦截"升级为「发现 → 拦截 → 漏网归因 → 迭代」的周度闭环。