#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周度：实锤造假账号 机审对照分析（过滤号店）
用法: python3 weekly_hammer_audit_compare.py --csv <标注CSV> --week wXX [--outdir DIR]

流程:
1. 标注CSV筛 remark_first='实锤造假' 账号
2. 查入驻表 apply_source，剔除号店(SELLER_PASS)
3. 逐uid调风控中枢命中日志接口取 proAccountQualificationSimResList + 处置结果
4. 实锤图 vs 送审图交集分类: same(工作流问题) / diff(图没进机审) / nofactor(向量检索问题)
5. 生成对照HTML + 汇总JSON
"""
import argparse, csv, json, os, sys, time, datetime, html as H
from collections import defaultdict, Counter

# 优先使用内网 risk-hitlog-factor skill 的 fetch_factor；缺依赖时回退到同目录可读桩
_try_skill = os.path.expanduser(os.environ.get('RISK_HITLOG_SKILL_DIR', '~/.openclaw/workspace/skills/risk-hitlog-factor/scripts'))
try:
    sys.path.insert(0, _try_skill)
    import fetch_factor as ff  # noqa: E402  -- 内部风控命中日志接口
except (ImportError, ModuleNotFoundError):
    import fetch_factor_stub as ff  # noqa: E402  -- 可读桩（见 scripts/fetch_factor_stub.py）

WORKDIR = os.environ.get('WORKDIR', os.path.expanduser('~/tmp'))


def run_sql(code):
    """direct-sr 查询返回stdout文本"""
    import subprocess
    r = subprocess.run(['dp', 'dataverse', 'sql', 'direct-sr', '--code', code],
                       capture_output=True, text=True, timeout=300)
    out = r.stdout
    if r.returncode != 0 or 'ERROR' in out:
        raise RuntimeError(out[-500:])
    return out


def parse_sql_table(out):
    """解析 direct-sr markdown表格 → [dict]"""
    lines = [l for l in out.splitlines() if l.startswith('|')]
    if len(lines) < 2:
        return []
    header = [c.strip() for c in lines[0].strip('|').split('|')]
    rows = []
    for l in lines[1:]:
        if '---' in l:
            continue
        vals = [c.strip() for c in l.strip('|').split('|')]
        rows.append(dict(zip(header, vals)))
    return rows


def get_apply_sources(uids):
    """入驻表查apply_source（initAudit口径最新一条）"""
    uid_list = ','.join(f"'{u}'" for u in uids)
    code = f"""SELECT user_id, apply_source, pro_settle_time FROM (
  SELECT user_id, apply_source, pro_settle_time,
         row_number() OVER (PARTITION BY user_id ORDER BY pro_settle_tms DESC) AS rn
  FROM warehouse.dwd_seller_settle_df
  WHERE dtm = '{{ds_nodash}}' AND user_apply_type='initAudit'
    AND user_id IN ({uid_list})
) t WHERE rn=1"""
    out = run_sql(code)
    return {r['user_id']: r for r in parse_sql_table(out)}


def fetch_audit_records(uids, log):
    cookie = ff.load_cookie()
    out = {}
    for i, uid in enumerate(uids, 1):
        cond = {
            "startTime": ff.parse_time('2026-08-10') if False else int((time.time() - 21 * 86400) * 1000),
            "endTime": int(time.time() * 1000),
            "includedScenarioId": ["professionalAccountAudit", "componentAudit-HG"],
            "excludedScenarioId": [], "showFactors": ["proAccountQualificationSimResList"],
            "intervalForStat": 0, "fetchMoreDetail": True, "notVisibleColumns": [],
            "visibleColumns": ["proAccountQualificationSimResList", "riskId", "businessId", "userId",
                                "scenarioId", "historyId", "auditStartTime", "finalHandleResult"],
            "timeType": "dynamic", "pageSize": 60, "curPage": 1, "userId": uid,
        }
        try:
            d = ff.call(cond, cookie)
            out[uid] = d.get('data', {}).get('records') or []
        except Exception as e:
            out[uid] = []
            print(f"{i}/{len(uids)} {uid} ERR {e}", flush=True)
        print(f"{i}/{len(uids)} {uid[:10]} recs={len(out[uid])}", flush=True)
        time.sleep(0.4)
    return out


def build_cards(hammer_rows, apply_info, audit, info):
    cards = []
    for r in hammer_rows:
        u = r['user_id']
        agr = defaultdict(dict)
        for rec in audit.get(u, []):
            key = (rec.get('businessId'), rec.get('auditStartTime'))
            if key not in agr:
                agr[key] = {'order': rec.get('businessId'), 't': rec.get('auditStartTime'),
                            'scen': rec.get('scenarioId'), 'acts': set(), 'tags': set(), 'pairs': []}
            for p in (rec.get('hitProcessRecords') or []):
                agr[key]['acts'].add(p.get('scenarioProcessName') or p.get('strategyName') or '')
                jd = p.get('jsonData')
                if jd:
                    try:
                        dd = json.loads(jd)
                        if dd.get('tagName'):
                            agr[key]['tags'].add(dd['tagName'])
                    except Exception:
                        pass
            v = (rec.get('showFactorValues') or {}).get('proAccountQualificationSimResList')
            if v and v != '<<<VALUE_NOT_EXIST>>>':
                try:
                    arr = json.loads(v)
                    if isinstance(arr, list) and arr:
                        agr[key]['pairs'] = [{'q': e['query_img_url'], 's': e['sim_img_url'],
                                              'p': round(e.get('pixel_similarity', 0), 4),
                                              'c': round(e.get('cosine_similarity', 0), 4),
                                              't': e.get('type_name', '')} for e in arr]
                except Exception:
                    pass
        audits = []
        for key, a in agr.items():
            audits.append({'time': datetime.datetime.fromtimestamp(a['t'] / 1000).strftime('%m-%d %H:%M') if a['t'] else '',
                           'order': a['order'], 'scen': a['scen'], 'acts': sorted(a['acts']),
                           'tags': sorted(a['tags']), 'pairs': a['pairs']})
        audits.sort(key=lambda x: x['time'])
        fake_urls = {r2['qualification_url'] for r2 in hammer_rows if r2['user_id'] == u}
        q_urls = {p['q'] for a in audits for p in a['pairs']}
        n_same = len(fake_urls & q_urls)
        if not q_urls:
            match = 'nofactor'
        elif n_same:
            match = 'same'
        else:
            match = 'diff'
        ap = apply_info.get(u, {})
        cards.append({'uid': u, 'short': u[:8], 'info': {'src': ap.get('apply_source', ''),
                                                          'trade': r.get('trade_first_name', ''),
                                                          'settle': ap.get('pro_settle_time', '')},
                      'ev': [{'url': r2['qualification_url'], 'verdict': r2['remark_first'],
                              'method': r2['remark_second'], 'cluster': r2['cluster_id']}
                             for r2 in hammer_rows if r2['user_id'] == u],
                      'audits': audits, 'match': match,
                      'n_same': n_same, 'n_fake': len(fake_urls), 'n_q': len(q_urls),
                      'n_orders': len(audits)})
    return cards


def esc(s):
    return H.escape(s or '')


def sc(p):
    if p >= 0.98: return 'red'
    if p >= 0.95: return 'orange'
    return 'yellow'


def build_html(cards, week):
    cnt = Counter(c['match'] for c in cards)
    body = ""
    order = {'same': 0, 'diff': 0, 'nofactor': 0}
    for idx, c in enumerate(cards, 1):
        i = c['info']
        order[c['match']] += 1
        evhtml = ""
        for e in c['ev']:
            vc = 'v-fake' if e['verdict'] == '实锤造假' else ('v-sus' if e['verdict'] == '疑似造假' else 'v-ok')
            evhtml += f"""<figure><figcaption><span class="{vc}">{esc(e['verdict'])}</span> {esc(e['method'])} · 簇{esc(e['cluster'])}</figcaption>
<img loading="lazy" src="{e['url']}" onerror="this.closest('figure').classList.add('err')"><div class="errtip">图片加载失败</div></figure>"""
        if c['match'] == 'nofactor':
            audhtml = f'<div class="nofac">机审因子未命中（共{c["n_orders"]}轮审核，相似检索均无结果）→ <b>算法向量检索问题</b></div>'
        else:
            plain = [a for a in c['audits'] if not a['tags'] and not a['pairs']]
            rich = [a for a in c['audits'] if a['tags'] or a['pairs']]
            audhtml = ""
            for n, a in enumerate(rich, 1):
                tags = ' '.join(f'<span class="tag rej">{esc(t)}</span>' for t in a['tags'])
                acts = ' '.join(f'<span class="tag">{esc(x)}</span>' for x in a['acts'][:3])
                pairs = ""
                for p in a['pairs']:
                    pairs += f"""<div class="pair">
<figure><figcaption>机审送审图 query</figcaption><img loading="lazy" src="{p['q']}" onerror="this.closest('figure').classList.add('err')"><div class="errtip">加载失败</div></figure>
<figure><figcaption>机审命中底图 sim</figcaption><img loading="lazy" src="{p['s']}" onerror="this.closest('figure').classList.add('err')"><div class="errtip">加载失败</div></figure>
<div class="pmeta"><span class="score {sc(p["p"])}">pixel {p["p"]:.4f}</span><span class="score dim">cos {p["c"]:.4f}</span><span class="tag">{esc(p["t"])}</span></div></div>"""
                audhtml += f"""<div class="auditblk"><div class="ahead"><b>审核#{n}</b> {esc(a['time'])} 单{esc(a['order'])} <span class="tag dim">{esc(a['scen'])}</span> {tags} {acts}</div>{pairs}</div>"""
            if plain:
                times = esc(plain[0]['time']) + ' ~ ' + esc(plain[-1]['time'])
                orders = '、'.join('单' + esc(a['order']) for a in plain[:4]) + ('…' if len(plain) > 4 else '')
                audhtml += f'<div class="nofac">其余 {len(plain)} 轮普通通过（{times}，{orders}，无命中无标签）</div>'
        if c['match'] == 'same':
            badge = '<span class="badge same">实锤图 = 送审图 → 大模型工作流问题</span>'
        elif c['match'] == 'diff':
            badge = '<span class="badge diff">实锤图 ≠ 送审图 → 图未进机审</span>'
        else:
            badge = '<span class="badge none">机审因子无命中 → 算法向量检索问题</span>'
        body += f"""<section class="card" data-match="{c['match']}">
<div class="chead"><span class="num">{idx}</span><span class="uid" style="font-size:13px;letter-spacing:0">{esc(c['uid'])}</span><span class="tag">{esc(i.get('src',''))}</span><span class="tag">{esc(i.get('trade',''))}</span>
<span class="tag settle">入驻 {esc(i.get('settle',''))}</span><span class="tag">机审{c['n_orders']}次</span>{badge}<span class="tag dim">实锤图{c["n_fake"]}张 · 送审图{c["n_q"]}张 · 相同{c["n_same"]}张</span></div>
<div class="cols">
 <div class="col"><h3>评估侧（聚簇标注证据图）</h3><div class="grid">{evhtml}</div></div>
 <div class="col"><h3>机审侧（因子命中 + 处置结果）</h3>{audhtml}</div>
</div></section>"""
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{week} 实锤造假 · 实锤图 vs 机审图对照</title><style>
:root{{--bg:#0e1014;--card:#171a21;--line:#272c36;--tx:#e9ebf0;--sub:#98a1b0;--red:#e5484d;--orange:#f76b15;--yellow:#f5d90a;--green:#30a46c}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);font:14px/1.55 -apple-system,"PingFang SC",sans-serif;padding:20px}}
h1{{font-size:20px}} h3{{font-size:13px;color:var(--sub);margin-bottom:8px;font-weight:600}}
.sub{{color:var(--sub);font-size:13px;margin:6px 0 14px}}
.bar{{display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:16px;z-index:9}}
button{{background:var(--card);border:1px solid var(--line);color:var(--sub);padding:6px 14px;border-radius:18px;cursor:pointer;font-size:13px}}
button.on{{background:#fff;color:#111;border-color:#fff}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:18px}}
.chead{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}}
.num{{background:#2a3140;color:#c6cfdd;border-radius:6px;font-size:12px;font-weight:700;padding:2px 8px}}
.uid{{font-weight:700;color:#7ab8ff}}
.tag{{background:#22262f;color:var(--sub);border-radius:6px;padding:2px 8px;font-size:12px}}
.tag.dim{{opacity:.75}} .tag.settle{{color:#8fd9b6}} .tag.rej{{background:rgba(229,72,77,.16);color:var(--red)}}
.badge{{border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600}}
.badge.same{{background:rgba(48,163,108,.18);color:var(--green)}}
.badge.diff{{background:rgba(247,107,21,.18);color:var(--orange)}}
.badge.none{{background:#22262f;color:var(--sub)}}
.cols{{display:grid;grid-template-columns:1fr 1.35fr;gap:16px}}
.col{{min-width:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
figure{{position:relative;background:#0a0c10;border-radius:8px;overflow:hidden;min-height:120px}}
figcaption{{position:absolute;top:0;left:0;right:0;background:rgba(0,0,0,.6);font-size:11px;padding:3px 6px;z-index:2}}
img{{width:100%;display:block;cursor:zoom-in}}
figure.err img{{display:none}} .errtip{{display:none;position:absolute;inset:0;place-items:center;color:var(--sub);font-size:11px}}
figure.err .errtip{{display:grid}}
.v-fake{{color:var(--red);font-weight:700}} .v-sus{{color:var(--yellow)}} .v-ok{{color:var(--green)}}
.auditblk{{border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:10px}}
.ahead{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;margin-bottom:8px}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}}
.pmeta{{grid-column:1/-1;display:flex;gap:6px;flex-wrap:wrap}}
.nofac{{color:var(--sub);font-size:12px;background:#0a0c10;border-radius:6px;padding:10px}}
.score{{font-size:12px;font-weight:600;border-radius:6px;padding:2px 8px}}
.score.red{{background:rgba(229,72,77,.15);color:var(--red)}}
.score.orange{{background:rgba(247,107,21,.15);color:var(--orange)}}
.score.yellow{{background:rgba(245,217,10,.12);color:var(--yellow)}}
.score.dim{{background:#22262f;color:var(--sub)}}
@media(max-width:900px){{.cols{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>{week} 实锤造假 · 实锤图 vs 机审图对照（已过滤号店）</h1>
<div class="sub">实锤图=送审图 → 机审见过且命中但放行（大模型工作流问题）· 实锤图≠送审图 → 图未进机审 · 因子无值 → 算法向量检索问题 · 送审图来源 proAccountQualificationSimResList.query_img_url</div>
<div class="bar"><button class="on" data-f="">全部 {len(cards)}</button>
<button data-f="same">实锤图=送审图 {cnt.get('same',0)}</button>
<button data-f="diff">实锤图≠送审图 {cnt.get('diff',0)}</button>
<button data-f="nofactor">因子无值 {cnt.get('nofactor',0)}</button></div>
{body}
<script>
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('.bar button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const f=b.dataset.f;
 document.querySelectorAll('.card').forEach(c=>c.style.display=(!f||c.dataset.match===f)?'':'none');}});
document.body.addEventListener('click',e=>{{
 const img=e.target.closest('img');if(!img)return;
 const o=document.createElement('div');
 o.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:99;display:grid;place-items:center;cursor:zoom-out';
 const c=img.cloneNode();c.style.cssText='max-width:94vw;max-height:94vh;width:auto;height:auto;object-fit:contain';o.appendChild(c);
 o.onclick=()=>o.remove();document.body.appendChild(o);}});
</script></body></html>"""
    return html, cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True, help='聚簇标注CSV路径')
    ap.add_argument('--week', required=True, help='周标签 如 w35')
    ap.add_argument('--outdir', default=None)
    args = ap.parse_args()
    outdir = args.outdir or os.path.join(WORKDIR, f'{args.week}_hammer_audit')
    os.makedirs(outdir, exist_ok=True)

    # 1. 实锤账号
    rows = list(csv.DictReader(open(args.csv, encoding='utf-8-sig')))
    hammer = [r for r in rows if r.get('remark_first') == '实锤造假']
    uids = sorted({r['user_id'] for r in hammer})
    print(f'实锤账号: {len(uids)}', flush=True)

    # 2. 过滤号店
    apply_info = get_apply_sources(uids)
    kept = [u for u in uids if apply_info.get(u, {}).get('apply_source') != 'SELLER_PASS']
    dropped = [u for u in uids if u not in kept]
    print(f'过滤号店 {len(dropped)} 个，剩余 {len(kept)}', flush=True)
    hammer_kept = [r for r in hammer if r['user_id'] in kept]

    # 3. 机审记录
    audit = fetch_audit_records(kept, None)

    # 4. 组装
    cards = build_cards(hammer_kept, apply_info, audit, None)
    # 5. HTML
    html, cnt = build_html(cards, args.week)
    html_path = os.path.join(outdir, f'{args.week}_hammer_vs_audit.html')
    open(html_path, 'w').write(html)
    json.dump({'cards': cards, 'cnt': dict(cnt), 'seller_dropped': dropped},
              open(os.path.join(outdir, f'{args.week}_summary.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({'week': args.week, 'hammer_total': len(uids), 'seller_dropped': len(dropped),
                      'kept': len(kept), 'same_工作流问题': cnt.get('same', 0),
                      'diff_图未进机审': cnt.get('diff', 0), 'nofactor_向量检索问题': cnt.get('nofactor', 0),
                      'html': html_path}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
