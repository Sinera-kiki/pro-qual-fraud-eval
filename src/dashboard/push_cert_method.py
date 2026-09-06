#!/usr/bin/env python3
"""
认证方式分布推送（cert_method，2026-08-21 定稿）

接口：POST /api/push/runs/{run_id}/cert_method
Payload: {"items":[{"industry","method","registered_count","violated_count"}]}

口径（与用户 2026-08-21 定稿一致）：
  - 分母 registered_count = 当周新入驻账户按认证方式：settle 表 initAudit+cert 2/4
    +first_trade_name+uid 去重 → biz_org_info dtm=<biz-date> 快照 auth
  - 分子 violated_count = 当周抽样×实锤×行业匹配的 uid → 同一 biz_org_info 分区快照 auth
    （分子分母必须同时点，否则跨天分区可能取到 legalPersonPhone 等分母没有的枚举）
  - method 用中文（法人人脸/对公打款/认证公函/客户对平台打款/法人手机号）
  - W<week> run=1 是基线周（is_baseline=true），不推

依赖：本脚本用 warehouse-cli dataverse sql direct-engine 现场查数据，不依赖预生成 CSV。

用法：
  python push_cert_method.py --run-id N --week-key wXX --industry 商务服务 \
      --sample-match-file wXX/matched_overall.csv         # W<week> 格式，或
      --sample-match-file wXX/match_sample_annotated_大盘整体.csv  # W<week> 格式，或
      --sample-match-xlsx  wXX/W33_抽样标注匹配结果_MMDD-MMDD.xlsx  # W<week> 格式
      [--dry-run]
"""
import os
import argparse, csv, json, ssl, subprocess, sys, urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

BASE = "https://dashboard.example.com/pro-qual-eval"
TOKEN = os.environ.get("DASH_PUSH_TOKEN", "")
CA = "${HOME}/.openclaw/mitm-proxy/ca-cert.pem"

METHOD_CN = {
    'legalPersonInformation': '法人人脸',
    'legalPersonPhone': '法人手机号',
    'publicPayment': '对公打款',
    'customerPayment': '客户对平台打款',
    'businessLetter': '认证公函',
}


def post(path, payload):
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(CA)
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"X-Push-Token": TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def query_run_info(run_id):
    """从看板 GET /api/runs/{id} 拿 run 元信息 (week_start, week_end, is_baseline)"""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(CA)
    req = urllib.request.Request(
        f"{BASE}/api/runs/{run_id}",
        headers={"X-Push-Token": TOKEN},
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        d = json.loads(r.read().decode("utf-8"))
        run = d.get('run', d)
        return run['week_start'], run['week_end'], bool(run.get('is_baseline', False))


def run_sql(sql, hive_only=False):
    """submit SQL 异步 + 轮询取结果（direct-engine 对本表 JOIN 会超时）"""
    import time as _t
    last_err = None
    for attempt in range(3):  # submit 偶发无 msgId，重试 3 次
        r = subprocess.run(['dp','dataverse','sql','submit','--code',sql,'--language','SQL'],
                           capture_output=True, text=True, timeout=300)
        msg_id = None
        for ln in r.stdout.splitlines():
            if 'msgId:' in ln:
                msg_id = ln.split('msgId:')[1].strip()
                break
        if msg_id:
            break
        last_err = f'submit 无 msgId: {r.stdout[-800:]} {r.stderr[-300:]}'
        print(f'⚠️ {last_err}，第 {attempt+1} 次重试...')
        _t.sleep(10)
    else:
        raise RuntimeError(last_err or 'submit 无 msgId')
    import time
    status_fail = 0  # 连续 status 命令自身失败次数（区别于任务失败）
    for _ in range(60):  # 最多 ~15min
        time.sleep(15)
        try:
            s = subprocess.run(['dp','dataverse','sql','status','--msg-id',msg_id],
                               capture_output=True, text=True, timeout=120)
            status_fail = 0
        except subprocess.TimeoutExpired:
            status_fail += 1
            print(f'⚠️ status 轮询命令超时(120s)，msgId={msg_id}，累计 {status_fail} 次，重试...')
            if status_fail >= 3:
                raise RuntimeError(f'status 连续 3 次命令超时，msgId={msg_id}')
            continue
        if 'SUCCESS' in s.stdout or 'FINISHED' in s.stdout: break
        if 'FAILED' in s.stdout or 'CANCELLED' in s.stdout:
            raise RuntimeError(f'submit 失败 msgId={msg_id}: {s.stdout[-500:]}')
    else:
        raise RuntimeError(f'submit 超时 msgId={msg_id}')
    res = subprocess.run(['dp','dataverse','sql','result','--msg-id',msg_id,'--raw'],
                         capture_output=True, text=True, timeout=60)
    txt = res.stdout
    lines = txt.splitlines()
    for k, ln in enumerate(lines):
        if ln.startswith('{'):
            body = '\n'.join(lines[k:])
            obj = json.loads(body)
            return obj.get('dataList') or obj.get('result',{}).get('dataList', [])
    raise RuntimeError(f'result 无 JSON: {txt[-500:]}')


def load_sample_hammer_uids(csv_file, xlsx_file, industry):
    """加载抽样×实锤 uid（三周格式各异）"""
    uids = set()
    if csv_file:
        with open(csv_file, encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        # W<week> 列：trade_first_name / remark_first_set
        # W<week> 列：first_trade_name / remark_first
        header = rows[0].keys() if rows else []
        ind_col = 'trade_first_name' if 'trade_first_name' in header else 'first_trade_name'
        rem_col = 'remark_first_set' if 'remark_first_set' in header else 'remark_first'
        for r in rows:
            if r[ind_col] == industry and r[rem_col] == '实锤造假':
                uids.add(r['user_id'])
    if xlsx_file:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_file)
        ws = wb['大盘抽样']
        rows = list(ws.iter_rows(values_only=True))
        header = rows[0]
        i_uid = header.index('user_id')
        i_ind = header.index('first_trade_name')
        i_rem = header.index('remark_first')
        for r in rows[1:]:
            if r[i_ind] == industry and r[i_rem] == '实锤造假':
                uids.add(r[i_uid])
    return uids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--industry", default='商务服务')
    ap.add_argument("--sample-match-file", help='W<week>/W<week> CSV 抽样×实锤匹配表')
    ap.add_argument("--sample-match-xlsx", help='W<week>+ xlsx 匹配表（含"大盘抽样" sheet）')
    ap.add_argument("--biz-date", help='biz_org_info 快照分区，默认取"当前 T-1"格式yyyyMMdd')
    ap.add_argument("--settle-dtm", help='settle 分区（历史周回补用当周日分区，默认同 biz-date；settle 分区会滚动，周日+4 的分区当周数据可能不全，2026-08-26 实测 W<week> 的 0806 分区只剩零头）')
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.sample_match_file and not args.sample_match_xlsx:
        sys.exit('必须提供 --sample-match-file 或 --sample-match-xlsx')

    # 从看板拿 week 区间（防止 run_id 与周次记错，参考 registered_source 教训）
    ws, we, is_baseline = query_run_info(args.run_id)
    print(f'run {args.run_id} 周区间: {ws} ~ {we} (is_baseline={is_baseline})')

    # 防呆 E：基线 run 不推
    if is_baseline:
        sys.exit(f'run {args.run_id} 是基线 run（is_baseline=true），不推 cert_method')

    # biz_date：默认取周日+4=周四，但要防 T+1 未落分区——先试算候选、找第一个有数据的分区
    # 候选：周日+4 → +3 → +2 → +1（从充裕到紧凑），不足今天 T-1 时才停
    if args.biz_date:
        biz_dt = args.biz_date
    else:
        y,m,d = map(int, we.split('-'))
        we_date = date(y,m,d)
        today = date.today()
        biz_dt = None
        for delta in [4, 3, 2, 1]:
            cand = we_date + timedelta(days=delta)
            if cand >= today: continue  # 不能取今天/未来（T+1 未落）
            cand_str = cand.strftime('%Y%m%d')
            # 探分区行数
            probe_sql = f"SELECT COUNT(*) AS c FROM warehouse.ods_professional_biz_org_info WHERE dtm='{cand_str}'"
            try:
                rows = run_sql(probe_sql)
                cnt = int(rows[0]['c']) if rows else 0
                if cnt > 0:
                    biz_dt = cand_str
                    print(f'biz_org_info 分区探测: dtm={cand_str} 有 {cnt} 行，采用')
                    break
                else:
                    print(f'biz_org_info 分区探测: dtm={cand_str} 为 0，往前退一天')
            except Exception as e:
                print(f'biz_org_info 分区探测: dtm={cand_str} 报错 {e}，往前退')
        if not biz_dt:
            sys.exit(f'❌ 周日 {we} 后 4 天内 biz_org_info 都无分区，等 T+1 落数据再跑')
    print(f'biz_org_info 快照分区: dtm={biz_dt}')

    hammer_uids = load_sample_hammer_uids(args.sample_match_file, args.sample_match_xlsx, args.industry)
    print(f'{args.industry} 抽样×实锤 uid: {len(hammer_uids)}')
    if not hammer_uids:
        print(f'⚠️ {args.industry} 抽样×实锤为 0——可能是该行业无实锤，或匹配文件格式不对')

    # 分母：settle 当周新入驻 join biz_org_info 快照
    settle_dtm = getattr(args, "settle_dtm", None) or biz_dt
    if settle_dtm != biz_dt:
        print(f'settle 分区（历史回补）: dtm={settle_dtm}')
    sql_denom = f"""
SELECT COALESCE(NULLIF(i.authentication_type,''),'未取到') AS auth, COUNT(DISTINCT n.user_id) AS cnt
FROM (SELECT DISTINCT user_id FROM warehouse.dwd_seller_settle_df
      WHERE dtm='{settle_dtm}' AND pro_settle_date BETWEEN '{ws}' AND '{we}'
        AND user_apply_type='initAudit' AND certificate_type IN (2,4)
        AND first_trade_name='{args.industry}') n
LEFT JOIN (SELECT user_id, authentication_type FROM warehouse.ods_professional_biz_org_info
           WHERE dtm='{biz_dt}') i
ON n.user_id=i.user_id
GROUP BY COALESCE(NULLIF(i.authentication_type,''),'未取到')
"""
    denom = {r['auth']: int(r['cnt']) for r in run_sql(sql_denom)}
    print(f'分母（{sum(denom.values())} 户）: {denom}')

    # 分子：hammer_uids 在同分区的 auth
    numer = defaultdict(int)
    if hammer_uids:
        ul = ','.join(f"'{u}'" for u in hammer_uids)
        sql_num = f"""
SELECT user_id, authentication_type FROM warehouse.ods_professional_biz_org_info
WHERE dtm='{biz_dt}' AND user_id IN ({ul})
"""
        rows = run_sql(sql_num)
        got = {r['user_id']: r['authentication_type'] for r in rows}
        for u in hammer_uids:
            a = got.get(u, '未取到')
            numer[a or '未取到'] += 1
    print(f'分子（{sum(numer.values())} 户）: {dict(numer)}')

    # ============ 固化自检 ============
    # C1: 分子未取到不能有——若有，说明实锤 uid 在 biz_org_info 快照里查不到
    if numer.get('未取到', 0) > 0:
        raise RuntimeError(
            f'❌ 分子有 {numer["未取到"]} 户在 biz_org_info dtm={biz_dt} 快照里查不到 '
            f'authentication_type。可能是账号已注销，需改回溯入驻日分区。停下不推。'
        )
    # C2: 分子 auth 桶必须都在分母桶里，否则会出现 registered=0 / violated>0 幽灵行
    ghost = set(numer) - set(denom)
    if ghost:
        raise RuntimeError(
            f'❌ 分子出现分母没有的认证方式: {ghost}。这会造成看板 registered=0 幽灵行。'
            f'（很可能分子分母不同分区导致，检查 --biz-date）停下不推。'
        )
    # C3: 分子总数应等于抽样×实锤 uid 数（除非 C1 命中）
    if sum(numer.values()) != len(hammer_uids):
        raise RuntimeError(
            f'❌ 分子合计 {sum(numer.values())} ≠ 抽样×实锤 uid {len(hammer_uids)}。停下不推。'
        )
    # C4: 分母未取到占比不应超过 5%（feedback_weekly_selfcheck_and_codify.md 精神）
    denom_missing = denom.get('未取到', 0)
    denom_total = sum(denom.values())
    if denom_total and denom_missing / denom_total > 0.05:
        print(f'⚠️ 分母未取到 {denom_missing}/{denom_total} ({denom_missing/denom_total:.1%})，'
              f'超过 5% 阈值——biz_org_info dtm={biz_dt} 分区可能不合适，检查是否 T+1 延迟')

    # 组装 items（覆盖分母全部 auth 键 + 分子额外键）
    all_methods = set(denom) | set(numer)
    items = []
    for m in all_methods:
        if m == '未取到': continue  # 不推"未取到"
        items.append({
            "industry": args.industry,
            "method": METHOD_CN.get(m, m),
            "registered_count": int(denom.get(m, 0)),
            "violated_count": int(numer.get(m, 0)),
        })
    items.sort(key=lambda x: -x['registered_count'])
    print(f'\n[run_id={args.run_id}] industry={args.industry}')
    for it in items:
        print(f"  {it['method']:12s} registered={it['registered_count']:>5} violated={it['violated_count']:>4}")

    if args.dry_run:
        print('[dry-run] 未推送')
        return

    status, resp = post(f"/api/push/runs/{args.run_id}/cert_method", {"items": items})
    print(f'\nHTTP {status}: {json.dumps(resp, ensure_ascii=False)[:400]}')
    if status != 200: raise SystemExit(1)


if __name__ == "__main__":
    main()
