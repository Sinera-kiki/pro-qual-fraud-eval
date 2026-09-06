#!/usr/bin/env bash
# 批量跑多日 7 天窗口聚簇
# 用法: bash run_multi_day.sh 20260804 20260802 20260801 20260731
set -u

BASE=${PROJECT_ROOT}
cd "$BASE"

export REQUESTS_CA_BUNDLE=${HOME}/.openclaw/mitm-proxy/ca-cert.pem
export IMG_DECODE_CONCURRENCY=6

for DS in "$@"; do
  # DS 形如 20260804
  Y=${DS:0:4}; M=${DS:4:2}; D=${DS:6:2}
  BIZ="$Y-$M-$D"
  START=$(date -d "$BIZ -6 days" +%Y-%m-%d)
  OUT="$BASE/multi_day/$DS"
  mkdir -p "$OUT"

  echo "=========================================="
  echo "[$(date '+%F %T')] START $BIZ  window=$START ~ $BIZ  dtm=$DS"
  echo "=========================================="

  if [ -s "$OUT/cluster.csv" ]; then
    echo "[skip] $DS 已有聚簇结果"
    continue
  fi

  # ---- 1. 取数 ----
  if [ ! -s "$OUT/emb.csv" ]; then
    cat > "$OUT/q.sql" <<SQLEOF
WITH base_user AS (
  SELECT cast(user_id AS string) AS user_id, pro_settle_date,
    CASE WHEN pro_settle_date='$BIZ' THEN true ELSE false END AS is_target_user,
    CASE WHEN pro_settle_date='$BIZ' THEN 'target_day' ELSE 'history_7d' END AS source_window
  FROM warehouse.dwd_seller_settle_df
  WHERE dtm='$DS' AND pro_settle_date BETWEEN '$START' AND '$BIZ'
    AND user_apply_type='initAudit' AND certificate_type IN (2,4)
  GROUP BY cast(user_id AS string), pro_settle_date,
    CASE WHEN pro_settle_date='$BIZ' THEN true ELSE false END,
    CASE WHEN pro_settle_date='$BIZ' THEN 'target_day' ELSE 'history_7d' END
),
trade_info AS (
  SELECT cast(user_id AS string) AS user_id, first_trade_name AS trade_first_name,
         second_trade_name AS trade_second_name
  FROM warehouse.dws_ecm_pro_account_qualification_full_governance_df
  WHERE dtm='$DS'
  GROUP BY cast(user_id AS string), first_trade_name, second_trade_name
),
embedding_info AS (
  SELECT cast(user_id AS string) AS user_id, qualification_url, max(embeds) AS embeds
  FROM warehouse.app_pro_account_ind_qualification_embedding_df
  WHERE dtm='$DS' AND status='success'
  GROUP BY cast(user_id AS string), qualification_url
)
SELECT b.user_id, t.trade_first_name, t.trade_second_name, e.qualification_url,
       e.embeds, b.pro_settle_date, b.is_target_user, b.source_window
FROM base_user b
LEFT JOIN trade_info t ON b.user_id=t.user_id
LEFT JOIN embedding_info e ON b.user_id=e.user_id
WHERE e.embeds IS NOT NULL AND e.embeds<>''
  AND e.qualification_url IS NOT NULL AND e.qualification_url<>''
SQLEOF

    echo "[$(date '+%T')] 提交取数..."
    MSG=$(warehouse-cli sql submit --mode DOWNLOAD --language sql --file "$OUT/q.sql" 2>&1 | grep -oP 'msgId:\s+\K[0-9a-f-]{36}' | head -1)
    if [ -z "$MSG" ]; then echo "[ERR] $DS 提交失败"; continue; fi
    echo "[$(date '+%T')] msgId=$MSG 等待完成..."

    for i in $(seq 1 90); do
      sleep 20
      ST=$(warehouse-cli dataverse sql status --msg-id "$MSG" 2>&1 | grep -oP '查询状态:\s+\K\w+' | head -1)
      [ "$ST" = "FINISHED" ] && break
      case "$ST" in ERROR|CANCELLED|KILLED|STOPPED) echo "[ERR] $DS 查询 $ST"; break;; esac
    done
    if [ "$ST" != "FINISHED" ]; then echo "[ERR] $DS 未完成($ST)"; continue; fi

    URL=$(warehouse-cli dataverse sql result --msg-id "$MSG" --raw 2>&1 | grep -oP 'https://xhs-data-engine[^ "]+\.csv\?[^ "]*' | head -1)
    if [ -z "$URL" ]; then echo "[ERR] $DS 无下载链接"; continue; fi
    echo "[$(date '+%T')] 下载..."
    curl -sL -o "$OUT/emb.csv" "$URL"
  fi

  ROWS=$(wc -l < "$OUT/emb.csv")
  echo "[$(date '+%T')] emb.csv rows=$ROWS"
  if [ "$ROWS" -lt 100 ]; then echo "[ERR] $DS 数据过少，跳过"; continue; fi

  # ---- 2. 聚簇 ----
  echo "[$(date '+%T')] 开始聚簇..."
  "$BASE/python" "$BASE/cluster_fast_v2.py" \
    --input-file "$OUT/emb.csv" \
    --method pixel_match \
    --url-col qualification_url --id-col user_id \
    --max-long-side 512 --orb-features 800 \
    --skip-align-cosine 0.99 --cosine-topk 5 --cosine-prefilter 0.98 \
    --match-rate-threshold 0.95 --pixel-tolerance 10 \
    --max-workers 16 \
    --output "$OUT/cluster.csv" \
    --suspect "$OUT/suspect.csv" > "$OUT/cluster.log" 2>&1

  echo "[$(date '+%T')] DONE $DS  cluster rows=$(wc -l < "$OUT/cluster.csv" 2>/dev/null || echo 0)"
  # 清理大文件省磁盘
  rm -f "$OUT/emb.csv"
done

echo "[$(date '+%F %T')] ALL DONE"
