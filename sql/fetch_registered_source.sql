-- 新入驻账户来源分布取数
-- 输出：first_trade_name, apply_source, uid_cnt   （供 push_registered_source.py 消费）
--
-- 口径（semantic_new_settle_account_scope）：
--   pro_settle_date ∈ [周一, 周日]，user_apply_type='initAudit'，certificate_type IN (2,4)
--   按 user_id 去重后统计。
--   行业名与 apply_source 均取 pro_settle_tms 最大那条（同 user_id 多次入驻时以最后一次为准）。
--   行业名直接来自入驻表 first_trade_name（用户入驻时手填的行业），不 JOIN 治理宽表——
--   宽表 dtm 分区稀疏（实测 0722-0810 期间完全无分区），历史周会拿不到；
--   入驻表逐条自带行业，可任意周回补。
--
-- 每周替换两处：
--   :dtm_start / :dtm_end     —— 分区区间（无横线 yyyyMMdd），覆盖整个周
--   :settle_start / :settle_end —— 入驻日期区间（带横线 yyyy-MM-dd）
--
-- 用法（direct-engine DOWNLOAD 模式）：
--   warehouse-cli sql submit --file fetch_registered_source.sql --mode DOWNLOAD --language sql
--   python push_registered_source.py --run-id N \
--       --source-file wXX/registered_source_raw.csv --dry-run

WITH ranked AS (
  SELECT
    cast(user_id AS string) AS user_id,
    apply_source,
    first_trade_name,
    row_number() OVER (
      PARTITION BY cast(user_id AS string)
      ORDER BY pro_settle_tms DESC, apply_id DESC
    ) AS rn
  FROM warehouse.dwd_seller_settle_df
  WHERE dtm BETWEEN :dtm_start AND :dtm_end
    AND pro_settle_date BETWEEN :settle_start AND :settle_end
    AND user_apply_type = 'initAudit'
    AND certificate_type IN (2, 4)
),
base_user AS (
  SELECT user_id, apply_source,
         CASE WHEN first_trade_name IS NULL OR first_trade_name = '' THEN '未知'
              ELSE first_trade_name END AS first_trade_name
  FROM ranked WHERE rn = 1
)
SELECT first_trade_name, apply_source, COUNT(DISTINCT user_id) AS uid_cnt
FROM base_user
GROUP BY first_trade_name, apply_source
ORDER BY uid_cnt DESC;
