#!/usr/bin/env python3
"""
认证方式分布 · 全行业合并推送（cert_method bulk，2026-08-28 定稿）

相对 push_cert_method.py 的差异：
  - 分母 SQL 从 24 条（每行业一条）合并成 1 条：GROUP BY first_trade_name, auth
  - 分子 SQL 从 24 条合并成 1 条：hammer uid 全集去 biz_org_info 同分区查 auth，
    然后本地按 (first_trade_name, auth) 汇总
  - 每条 SQL 内建 3 次重试；status 轮询超时放宽到 120s + 连续 3 次超时才放弃
  - 24 行业一次性 POST 给看板（接口本身就支持 items 混行业）

用法：
  python push_cert_method_bulk.py --run-id N \
      --sample-match-file wXX/match_all_cert.csv          # 老格式，或
      --sample-match-xlsx  wXX/WXX_抽样标注匹配结果_MMDD-MMDD.xlsx
      [--biz-date yyyymmdd] [--settle-dtm yyyymmdd] [--dry-run]

预计耗时：5~8 分钟；相比单行业串行的 1~2 小时提速 15 倍。
成功率：分母/分子各自命令级 3 次自动重试，能救回瞬时抖动。
Fallback：本脚本任一段 SQL 三次都失败时，脚本 exit 3，外层可回落到旧脚本
push_cert_method.py 逐行业跑。
"""
import os
import argparse, csv, json, ssl, subprocess, sys, time, urllib.request
from collections import defaultdict
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
    ctx = ssl.create_default_context(); ctx.load_verify_locations(CA)
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"X-Push-Token": TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def query_run_info(run_id):
    ctx = ssl.create_default_context(); ctx.load_verify_locations(CA)
    req = urllib.request.Request(f"{BASE}/api/runs/{run_id}", headers={"X-Push-Token": TOKEN})
    with urllib.request.urlopen(req, context=ctx) as r:
        d = json.loads(r.read().decode("utf-8"))
        run = d.get('run', d)
        return run['week_start'], run['week_end'], bool(run.get('is_baseline', False))


def run_sql_once(sql, label):
    """单次 SQL：submit → status 轮询 → result；命令级超时(120s status/60s result)。"""
    r = subprocess.run(['dp','dataverse','sql','submit','--code',sql,'--language','SQL'],
                       capture_output=True, text=True, timeout=300)
    msg_id = None
    for ln in r.stdout.splitlines():
        if 'msgId:' in ln:
            msg_id = ln.split('msgId:')[1].strip(); break
    if not msg_id:
        raise RuntimeError(f'{label} submit 无 msgId: {r.stdout[-500:]}')
    print(f'  {label} msgId={msg_id}', flush=True)
    status_fail = 0
    for _ in range(80):  # 最多 ~20min
        time.sleep(15)
        try:
            s = subprocess.run(['dp','dataverse','sql','status','--msg-id',msg_id],
                               capture_output=True, text=True, timeout=120)
            status_fail = 0
        except subprocess.TimeoutExpired:
            status_fail += 1
            print(f'  ⚠️ {label} status 轮询超时({status_fail}/3)，msgId={msg_id}', flush=True)
            if status_fail >= 3:
                raise RuntimeError(f'{label} status 连续 3 次命令超时 msgId={msg_id}')
            continue
        if 'SUCCESS' in s.stdout or 'FINISHED' in s.stdout: break
        if 'FAILED' in s.stdout or 'CANCELLED' in s.stdout:
            raise RuntimeError(f'{label} 任务失败 msgId={msg_id}: {s.stdout[-500:]}')
    else:
        raise RuntimeError(f'{label} 20min 未完成 msgId={msg_id}')
    res = subprocess.run(['dp','dataverse','sql','result','--msg-id',msg_id,'--raw'],
                         capture_output=True, text=True, timeout=120)
    txt = res.stdout
    for k, ln in enumerate(txt.splitlines()):
        if ln.startswith('{'):
            body = '\n'.join(txt.splitlines()[k:])
            obj = json.loads(body)
            return obj.get('dataList') or obj.get('result',{}).get('dataList', [])
    raise RuntimeError(f'{label} result 无 JSON: {txt[-500:]}')


def run_sql(sql, label, retries=3):
    last = None
    for a in range(retries):
        try:
            return run_sql_once(sql, label)
        except Exception as e:
            last = e
            print(f'  ⚠️ {label} 第 {a+1}/{retries} 次失败: {e}', flush=True)
            time.sleep(20)
    raise RuntimeError(f'{label} {retries} 次全部失败: {last}')


def load_all_hammer(csv_file, xlsx_file):
    """返回 {user_id -> first_trade_name}"""
    m = {}
    if csv_file:
        with open(csv_file, encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        if rows:
            header = rows[0].keys()
            ind_col = 'trade_first_name' if 'trade_first_name' in header else 'first_trade_name'
            rem_col = 'remark_first_set' if 'remark_first_set' in header else 'remark_first'
            for r in rows:
                if r[rem_col] == '实锤造假':
                    m[r['user_id']] = r[ind_col]
    if xlsx_file:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_file)
        ws = wb['大盘抽样']
        rows = list(ws.iter_rows(values_only=True))
        header = rows[0]
        i_uid = header.index('user_id'); i_ind = header.index('first_trade_name'); i_rem = header.index('remark_first')
        for r in rows[1:]:
            if r[i_rem] == '实锤造假':
                m[r[i_uid]] = r[i_ind]
    return m


def probe_biz_partition(we, given_biz_date):
    if given_biz_date:
        return given_biz_date
    y, m, d = map(int, we.split('-'))
    we_date = date(y, m, d); today = date.today()
    for delta in [4, 3, 2, 1]:
        cand = we_date + timedelta(days=delta)
        if cand >= today: continue
        cand_str = cand.strftime('%Y%m%d')
        rows = run_sql(f"SELECT COUNT(*) AS c FROM warehouse.ods_professional_biz_org_info WHERE dtm='{cand_str}'",
                       f'probe {cand_str}', retries=2)
        cnt = int(rows[0]['c']) if rows else 0
        print(f'  分区探测 dtm={cand_str}: {cnt} 行', flush=True)
        if cnt > 0:
            return cand_str
    sys.exit(f'❌ 周日 {we} 后 4 天内 biz_org_info 都无分区')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--sample-match-file", help='W<week>/W<week> CSV 抽样×实锤匹配表')
    ap.add_argument("--sample-match-xlsx", help='W<week>+ xlsx 匹配表（含"大盘抽样" sheet）')
    ap.add_argument("--biz-date", help='biz_org_info 分区，默认自动探测')
    ap.add_argument("--settle-dtm", help='settle 分区（历史回补用当周日分区）')
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.sample_match_file and not args.sample_match_xlsx:
        sys.exit('必须提供 --sample-match-file 或 --sample-match-xlsx')

    ws, we, is_baseline = query_run_info(args.run_id)
    print(f'run {args.run_id} 周区间: {ws} ~ {we} (is_baseline={is_baseline})', flush=True)
    if is_baseline:
        sys.exit(f'run {args.run_id} 是基线 run，不推 cert_method')

    biz_dt = probe_biz_partition(we, args.biz_date)
    settle_dtm = args.settle_dtm or biz_dt
    print(f'biz_org_info 分区: dtm={biz_dt}  |  settle 分区: dtm={settle_dtm}', flush=True)

    # 1) 分母：全行业一次跑
    print('\n=== 分母（全行业合并）===', flush=True)
    sql_denom = f"""
SELECT n.first_trade_name AS industry,
       COALESCE(NULLIF(i.authentication_type,''),'未取到') AS auth,
       COUNT(DISTINCT n.user_id) AS cnt
FROM (SELECT user_id, first_trade_name
      FROM (SELECT user_id, first_trade_name,
                   ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY pro_settle_tms DESC) AS rn
            FROM warehouse.dwd_seller_settle_df
            WHERE dtm='{settle_dtm}' AND pro_settle_date BETWEEN '{ws}' AND '{we}'
              AND user_apply_type='initAudit' AND certificate_type IN (2,4)
              AND first_trade_name IS NOT NULL AND first_trade_name<>''
           ) t WHERE rn=1
     ) n
LEFT JOIN (SELECT user_id, authentication_type FROM warehouse.ods_professional_biz_org_info
           WHERE dtm='{biz_dt}') i
ON n.user_id=i.user_id
GROUP BY n.first_trade_name, COALESCE(NULLIF(i.authentication_type,''),'未取到')
"""
    denom_rows = run_sql(sql_denom, '分母SQL')
    denom = defaultdict(lambda: defaultdict(int))  # denom[industry][auth] = cnt
    for r in denom_rows:
        denom[r['industry']][r['auth']] += int(r['cnt'])
    industries_in_denom = set(denom.keys())
    print(f'分母覆盖 {len(industries_in_denom)} 行业，总户数 {sum(sum(v.values()) for v in denom.values())}', flush=True)

    # 2) 分子：hammer uid 全集去查 auth
    print('\n=== 分子（全行业合并）===', flush=True)
    uid_to_ind = load_all_hammer(args.sample_match_file, args.sample_match_xlsx)
    print(f'抽样×实锤 uid 总数: {len(uid_to_ind)}', flush=True)
    numer = defaultdict(lambda: defaultdict(int))
    if uid_to_ind:
        ul = ','.join(f"'{u}'" for u in uid_to_ind.keys())
        sql_num = f"""
SELECT user_id, authentication_type FROM warehouse.ods_professional_biz_org_info
WHERE dtm='{biz_dt}' AND user_id IN ({ul})
"""
        rows = run_sql(sql_num, '分子SQL')
        got = {r['user_id']: r['authentication_type'] for r in rows}
        for uid, ind in uid_to_ind.items():
            a = got.get(uid) or '未取到'
            numer[ind][a] += 1
    print(f'分子覆盖 {len(numer)} 行业，总户数 {sum(sum(v.values()) for v in numer.values())}', flush=True)

    # 3) 自检
    print('\n=== 自检 ===', flush=True)
    problems = []
    total_numer = 0
    for ind, aa in numer.items():
        if aa.get('未取到', 0) > 0:
            problems.append(f'{ind}: 分子有 {aa["未取到"]} 户在快照里查不到 auth')
        ghost = set(aa) - set(denom.get(ind, {}))
        if ghost:
            problems.append(f'{ind}: 分子出现分母没有的 auth {ghost}')
        total_numer += sum(aa.values())
    if total_numer != len(uid_to_ind):
        problems.append(f'分子合计 {total_numer} ≠ 抽样×实锤 uid 总数 {len(uid_to_ind)}')
    if problems:
        for p in problems: print('  ❌', p)
        sys.exit('自检未通过，停下不推')
    print('  ✅ 自检通过')

    # 4) 组装 items
    items = []
    for ind in sorted(industries_in_denom):
        methods = set(denom[ind]) | set(numer.get(ind, {}))
        for m in methods:
            if m == '未取到': continue
            items.append({
                "industry": ind,
                "method": METHOD_CN.get(m, m),
                "registered_count": int(denom[ind].get(m, 0)),
                "violated_count": int(numer.get(ind, {}).get(m, 0)),
            })
    items.sort(key=lambda x: (x['industry'], -x['registered_count']))
    print(f'\n准备推送 {len(items)} 条 items（{len(industries_in_denom)} 行业）', flush=True)

    if args.dry_run:
        print('[dry-run] 未推送。样例前 8 条：')
        for it in items[:8]: print(' ', it)
        return

    status, resp = post(f"/api/push/runs/{args.run_id}/cert_method", {"items": items})
    print(f'\nHTTP {status}: {json.dumps(resp, ensure_ascii=False)[:400]}', flush=True)
    if status != 200: sys.exit(1)


if __name__ == '__main__':
    main()
