-- 日跑取数模板：每天生成当日 SQL 时，把下面两处日期替换（分区 dtm 与 pro_settle_date 同一天）
-- 分区/业务日期：{{biz_date_nodash}} / {{biz_date}}
WITH
base_user AS (
  SELECT
    cast(user_id AS string) AS user_id,
    max(pro_settle_date) AS pro_settle_date,
    1 AS is_target_flag
  FROM warehouse.dwd_seller_settle_df
  WHERE dtm = '{{biz_date_nodash}}'
    AND pro_settle_date = '{{biz_date}}'
    AND user_apply_type = 'initAudit'
    AND certificate_type IN (2, 4)
  GROUP BY cast(user_id AS string)
),
trade_info AS (
  SELECT
    cast(t.user_id AS string) AS user_id,
    max(t.first_trade_name)  AS trade_first_name,
    max(t.second_trade_name) AS trade_second_name
  FROM (
    SELECT user_id, first_trade_name, second_trade_name
    FROM warehouse.dws_ecm_pro_account_qualification_full_governance_df
    WHERE dtm = '{{biz_date_nodash}}'
  ) t
  JOIN base_user b ON cast(t.user_id AS string) = b.user_id
  GROUP BY cast(t.user_id AS string)
),
embedding_info AS (
  SELECT
    cast(e.user_id AS string) AS user_id,
    e.qualification_url,
    max(e.embeds) AS embeds
  FROM warehouse.app_pro_account_ind_qualification_embedding_df e
  JOIN base_user b ON cast(e.user_id AS string) = b.user_id
  WHERE e.dtm = '{{biz_date_nodash}}'
    AND e.status = 'success'
  GROUP BY cast(e.user_id AS string), e.qualification_url
)
SELECT
  b.user_id,
  t.trade_first_name,
  t.trade_second_name,
  e.qualification_url,
  e.embeds,
  b.pro_settle_date,
  true AS is_target_user,
  'target_day' AS source_window
FROM base_user b
LEFT JOIN trade_info t   ON b.user_id = t.user_id
JOIN      embedding_info e ON b.user_id = e.user_id
WHERE e.embeds IS NOT NULL AND e.embeds <> ''
  AND e.qualification_url IS NOT NULL AND e.qualification_url <> ''
