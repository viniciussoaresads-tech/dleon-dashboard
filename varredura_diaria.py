# -*- coding: utf-8 -*-
"""Varredura Diaria — Agente leitor da Regua de Otimizacao v2 (D'Leon)

Camadas 0+1 da regua (memory/clients/dleon/processos/REGUA-OTIMIZACAO-CRIATIVOS-v2.md):
varre os anuncios ativos das 3 contas, aplica as bandas de CPL por celula
produto x canal e emite a LEITURA do dia — quem decide (pausa/escala) e o gestor.

Fontes:
  - Datalake Uno (facebook_campaign_data + deals): gasto, conversas, leads CRM,
    por anuncio/dia — mesma base validada do placar semanal.
  - Meta API (best effort): frequencia 7d por anuncio p/ flag de fadiga.

Medicao por canal:
  - mensagem: custo por CONVERSA (started_messages) — escala gerenciador.
  - cadastro: custo por LEAD CRM (datalake nao tem registro Meta) — teto
    convertido pela taxa de chegada da celula.

Saidas: data/varredura.json + varredura-diaria.html (+ resumo no console).
Agendado: Task Scheduler 'DLeon-Varredura-Diaria', dias uteis 08:00.
Somente leitura — o script NAO pausa nada.
"""
import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import psycopg2

ROOT = Path(__file__).parent
HOJE = date.today()

ACCOUNTS = {
    '926172801304741': ('Lentes', 'lentes'),
    '1543033206555581': ('Protocolo', 'protocolo'),
    '1137133676785413': ('HOF', 'hof'),
}

# ---- REGUA v2: bandas oficiais (2026-08-07) ------------------------------
# mensagem: medida em custo/conversa (escala gerenciador)
# cadastro: medida em custo/lead CRM (teto gerenciador / taxa de chegada)
CHEGADA = {('lentes', 'cadastro'): 0.58, ('protocolo', 'cadastro'): 0.66,
           ('hof', 'cadastro'): 0.59}

def _cad(prod, verde, teto, piso):
    t = CHEGADA[(prod, 'cadastro')]
    return {'verde': round(verde / t, 2), 'teto': round(teto / t, 2),
            'piso': round(piso / t, 2), 'metrica': 'lead CRM'}

BANDAS = {
    ('lentes', 'mensagem'):    {'verde': 6.50, 'teto': 11.50, 'piso': 3.25, 'metrica': 'conversa'},
    ('lentes', 'cadastro'):    _cad('lentes', 6.00, 13.00, 3.00),      # ~10,3 / 22,4 / 5,2
    ('protocolo', 'mensagem'): {'verde': 6.00, 'teto': 11.00, 'piso': 3.00, 'metrica': 'conversa'},
    ('protocolo', 'cadastro'): _cad('protocolo', 6.50, 13.00, 3.25),   # ~9,8 / 19,7 / 4,9
    ('hof', 'mensagem'):       {'verde': 4.50, 'teto': 9.50, 'piso': 2.25, 'metrica': 'conversa'},
    ('hof', 'cadastro'):       _cad('hof', 4.50, 9.50, 2.25),          # ~7,6 / 16,1 / 3,8
}
AMOSTRA_MIN = 30        # resultados p/ julgar custo (novo: acumulado; veterano: 14d)
AMOSTRA_JANELA = 15     # resultados minimos numa janela 7d isolada
IDADE_NOVO = 14         # ate aqui julga pelo acumulado; depois, janela movel
FREQ_FADIGA = 2.8
CHEGADA_MIN = 55.0      # % leads/conversas (so canal mensagem)


def funil_of(camp):
    c = (camp or '').upper()
    if re.search(r'CADASTRO|TYPEBO|QUIZ', c):
        return 'cadastro'
    if re.search(r'MSG|WHAT|DIRECT|MENSAGEM', c):
        return 'mensagem'
    return 'outros'


# ---- 1. Datalake: series diarias por anuncio (45d) -----------------------
BASE = HOJE - timedelta(days=45)
conn = psycopg2.connect(
    host='uno-datalake-cluster-1.cp74abrqalt6.sa-east-1.redshift.amazonaws.com',
    port=5439, dbname='uno', user='com8053_reader',
    password='4rCENOaB9c92m1I0cX5AHKmdMd3', sslmode='require', connect_timeout=60
)
cur = conn.cursor()
accs = tuple(ACCOUNTS.keys())

cur.execute("""
SELECT source_id, account_id, MAX(ad_name), MAX(ad_campaign_name),
       MIN(CASE WHEN total_spent>0 THEN date END)
FROM com8053.facebook_campaign_data
WHERE schema='clinica_dleon' AND account_id IN %(a)s
GROUP BY source_id, account_id
""", {'a': accs})
meta = {str(sid): {'acc': acc, 'ad': ad, 'camp': camp, 'pg': pg}
        for sid, acc, ad, camp, pg in cur.fetchall()}

cur.execute("""
SELECT source_id, date, ROUND(SUM(total_spent)::numeric,2), SUM(started_messages)
FROM com8053.facebook_campaign_data
WHERE schema='clinica_dleon' AND account_id IN %(a)s AND date >= %(b)s
GROUP BY source_id, date
""", {'a': accs, 'b': str(BASE)})
serie = {}
for sid, d, sp, conv in cur.fetchall():
    serie.setdefault(str(sid), {})[d] = [float(sp or 0), int(conv or 0), 0]

cur.execute("""
SELECT facebook_source_id, created_at::date, COUNT(*)
FROM com8053.deals
WHERE schema='clinica_dleon' AND created_at::date >= %(b)s
  AND facebook_source_id IS NOT NULL
GROUP BY 1,2
""", {'b': str(BASE)})
for sid, d, le in cur.fetchall():
    sid = str(sid)
    if sid in meta:
        serie.setdefault(sid, {}).setdefault(d, [0, 0, 0])[2] += int(le)
conn.close()

# ---- 2. Meta API (best effort): frequencia/CPM 7d por anuncio ------------
freq7 = {}
api_ok = False
try:
    mcp = json.loads(Path(r'C:\Users\vinic\.mcp.json').read_text(encoding='utf-8'))
    token = mcp['mcpServers']['agencia']['env']['META_DLEON_ACCESS_TOKEN']
    for acc_id in ACCOUNTS:
        params = urllib.parse.urlencode({
            'level': 'ad', 'fields': 'ad_id,frequency,cpm,ctr',
            'date_preset': 'last_7d', 'limit': 500, 'access_token': token,
            'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': 0}]),
        })
        url = f'https://graph.facebook.com/v21.0/act_{acc_id}/insights?{params}'
        with urllib.request.urlopen(url, timeout=60) as r:
            for row in json.loads(r.read()).get('data', []):
                freq7[row['ad_id']] = {'freq': float(row.get('frequency') or 0),
                                       'cpm': float(row.get('cpm') or 0),
                                       'ctr': float(row.get('ctr') or 0)}
    api_ok = True
except Exception as e:
    print(f"aviso: Meta API indisponivel ({e}) — camada 0 parcial", file=sys.stderr)

# ---- 3. Aplica a regua ---------------------------------------------------
D1 = HOJE - timedelta(days=1)
W1_INI, W1_FIM = HOJE - timedelta(days=7), HOJE - timedelta(days=1)   # janela fechada
W0_INI, W0_FIM = HOJE - timedelta(days=14), HOJE - timedelta(days=8)  # janela anterior


def soma(dd, ini, fim):
    sp = cv = le = 0
    for d, v in dd.items():
        if ini <= d <= fim:
            sp += v[0]; cv += v[1]; le += v[2]
    return sp, cv, le


leitura, celulas = [], {}
for sid, dd in serie.items():
    m = meta.get(sid)
    if not m or not m['pg']:
        continue
    acc_nome, prod = ACCOUNTS[m['acc']]
    canal = funil_of(m['camp'])
    sp3 = soma(dd, HOJE - timedelta(days=3), HOJE)[0]
    if sp3 <= 0:
        continue  # inativo
    idade = (HOJE - m['pg']).days

    if canal == 'outros':
        leitura.append({'sid': sid, 'ad': m['ad'], 'camp': m['camp'], 'conta': acc_nome,
                        'celula': f'{prod}|outros', 'idade': idade, 'status': 'INFO',
                        'acao': 'Campanha fora do funil direto (branding/evento) — sem banda',
                        'custo': None, 'gasto': round(soma(dd, W1_INI, W1_FIM)[0], 0)})
        continue

    b = BANDAS[(prod, canal)]
    res_i = 1 if b['metrica'] == 'conversa' else 2  # indice do resultado na serie

    # agregados p/ celula (resumo)
    k = f'{prod}|{canal}'
    c = celulas.setdefault(k, {'gasto7': 0, 'res7': 0, 'conv7': 0, 'leads7': 0,
                               'verde': b['verde'], 'teto': b['teto'], 'metrica': b['metrica']})
    sp, cv, le = soma(dd, W1_INI, W1_FIM)
    c['gasto7'] += sp; c['res7'] += (cv if res_i == 1 else le)
    c['conv7'] += cv; c['leads7'] += le

    if idade < IDADE_NOVO:  # ---- criativo NOVO: acumulado desde o inicio
        spA, cvA, leA = soma(dd, m['pg'], HOJE)
        res = cvA if res_i == 1 else leA
        custo = round(spA / res, 2) if res else None
        if idade < 3:
            status, acao = 'AGUARDANDO', f'D+{idade} — primeira leitura no D+3'
        elif res >= AMOSTRA_MIN and custo > b['teto']:
            status, acao = 'PAUSAR', f'Acumulado R$ {custo:.2f}/{b["metrica"]} > teto R$ {b["teto"]:.2f} com {res} resultados'
        elif res < AMOSTRA_MIN and spA >= 30 * b['teto']:
            status, acao = 'PAUSAR', f'Gastou R$ {spA:.0f} (>=30x teto) e gerou so {res} resultados'
        elif res >= AMOSTRA_MIN and custo < b['piso']:
            status, acao = 'AUDITAR', f'R$ {custo:.2f} abaixo do piso R$ {b["piso"]:.2f} — conferir qualidade antes de escalar'
        elif res < AMOSTRA_MIN:
            status, acao = 'AMOSTRA', f'{res}/{AMOSTRA_MIN} resultados — seguir observando'
        elif custo <= b['verde']:
            status, acao = 'VERDE', f'R$ {custo:.2f} <= verde R$ {b["verde"]:.2f} — candidato a escala apos validacao (D+14/20)'
        else:
            status, acao = 'AMARELO', f'R$ {custo:.2f} na banda amarela — manter ate leitura de agendamento'
        gasto = round(spA, 0)
    else:  # ---- criativo VETERANO: janela movel 7d fechada
        sp1, cv1, le1 = soma(dd, W1_INI, W1_FIM)
        sp0, cv0, le0 = soma(dd, W0_INI, W0_FIM)
        r1, r0 = (cv1, cv0) if res_i == 1 else (le1, le0)
        custo = round(sp1 / r1, 2) if r1 else None
        c0 = round(sp0 / r0, 2) if r0 else None
        if sp1 <= 0:
            continue
        if r1 >= AMOSTRA_JANELA and custo > 1.5 * b['teto']:
            status, acao = 'PAUSAR', f'Janela 7d R$ {custo:.2f}/{b["metrica"]} > 1,5x teto (R$ {1.5*b["teto"]:.2f})'
        elif r1 >= AMOSTRA_JANELA and custo > b['teto'] and c0 is not None and c0 > b['teto']:
            status, acao = 'PAUSAR', f'2 janelas seguidas acima do teto (R$ {c0:.2f} -> R$ {custo:.2f})'
        elif r1 == 0 and sp1 >= 15 * b['teto']:
            status, acao = 'PAUSAR', f'R$ {sp1:.0f} na semana sem nenhum resultado'
        elif r1 >= AMOSTRA_JANELA and custo > b['teto']:
            status, acao = 'ATENCAO', f'1a janela acima do teto (R$ {custo:.2f}) — pausa se repetir na proxima'
        elif r1 >= AMOSTRA_MIN and custo < b['piso']:
            status, acao = 'AUDITAR', f'R$ {custo:.2f} abaixo do piso — conferir taxa de agendamento'
        elif custo is not None and custo <= b['verde']:
            status, acao = 'VERDE', f'R$ {custo:.2f} <= verde — validar agendamento p/ escalar'
        elif custo is not None:
            status, acao = 'AMARELO', f'R$ {custo:.2f} na banda amarela'
        else:
            status, acao = 'AMOSTRA', f'{r1} resultados na janela — sem leitura'
        gasto = round(sp1, 0)

    fq = freq7.get(sid, {})
    fadiga = fq.get('freq', 0) >= FREQ_FADIGA
    if fadiga and status in ('VERDE', 'AMARELO'):
        acao += f' | FADIGA freq {fq["freq"]:.1f} — pedir criativo novo'
    leitura.append({'sid': sid, 'ad': (m['ad'] or '')[:70], 'camp': (m['camp'] or '')[:60],
                    'conta': acc_nome, 'celula': k, 'idade': idade, 'status': status,
                    'custo': custo, 'teto': b['teto'], 'metrica': b['metrica'],
                    'gasto': gasto, 'freq': fq.get('freq'), 'acao': acao,
                    'novo': idade < IDADE_NOVO})

# fadiga isolada (criativos ok de custo mas saturando)
fadigados = [x for x in leitura if x.get('freq') and x['freq'] >= FREQ_FADIGA]

# taxa de chegada por celula (so mensagem)
for k, c in celulas.items():
    c['chegada'] = round(100.0 * c['leads7'] / c['conv7'], 1) if k.endswith('mensagem') and c['conv7'] else None
    c['custo7'] = round(c['gasto7'] / c['res7'], 2) if c['res7'] else None
    c['gasto7'] = round(c['gasto7'], 0)

ORDEM = {'PAUSAR': 0, 'AUDITAR': 1, 'ATENCAO': 2, 'AMARELO': 3, 'AMOSTRA': 4,
         'AGUARDANDO': 5, 'VERDE': 6, 'INFO': 7}
leitura.sort(key=lambda x: (ORDEM.get(x['status'], 9), -(x['gasto'] or 0)))

out = {
    'gerado_em': str(HOJE), 'api_meta': api_ok,
    'resumo': {s: sum(1 for x in leitura if x['status'] == s) for s in ORDEM},
    'celulas': celulas, 'fadiga': len(fadigados), 'anuncios': leitura,
}
(ROOT / 'data' / 'varredura.json').write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')

# ---- 4. HTML da leitura --------------------------------------------------
CORES = {'PAUSAR': '#f87171', 'AUDITAR': '#fb923c', 'ATENCAO': '#fbbf24',
         'AMARELO': '#fbbf24', 'AMOSTRA': '#9aa3b5', 'AGUARDANDO': '#9aa3b5',
         'VERDE': '#34d399', 'INFO': '#60a5fa'}

def linha(x):
    cor = CORES.get(x['status'], '#9aa3b5')
    custo = f"R$ {x['custo']:.2f}" if x.get('custo') else '—'
    teto = f"R$ {x['teto']:.2f}" if x.get('teto') else '—'
    fr = f"{x['freq']:.1f}" if x.get('freq') else '—'
    tipo = 'NOVO' if x.get('novo') else ''
    return (f"<tr><td><span class='pill' style='background:{cor}22;color:{cor}'>{x['status']}</span></td>"
            f"<td class='adname'>{x['ad']}<br><small>{x['camp']}</small></td>"
            f"<td>{x['celula']}{' <b>' + tipo + '</b>' if tipo else ''}</td>"
            f"<td class='num'>{x['idade']}d</td><td class='num'>{custo}</td>"
            f"<td class='num'>{teto}</td><td class='num'>R$ {x['gasto']:.0f}</td>"
            f"<td class='num'>{fr}</td><td>{x['acao']}</td></tr>")

def cel_linha(k, c):
    ch = f"{c['chegada']:.0f}%" if c.get('chegada') else '—'
    alerta = ' ⚠' if c.get('chegada') and c['chegada'] < CHEGADA_MIN else ''
    cu = f"R$ {c['custo7']:.2f}" if c.get('custo7') else '—'
    return (f"<tr><td>{k}</td><td class='num'>R$ {c['gasto7']:.0f}</td>"
            f"<td class='num'>{cu}</td><td class='num'>R$ {c['teto']:.2f}</td>"
            f"<td class='num'>{ch}{alerta}</td><td>{c['metrica']}</td></tr>")

n = out['resumo']
html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Varredura Diária — {HOJE}</title>
<style>
:root{{--bg:#0b0e14;--card:#151a26;--card2:#1a2030;--txt:#e8ecf4;--txt2:#9aa3b5;--line:#242c3e}}
[data-theme=light]{{--bg:#f4f6fa;--card:#fff;--card2:#f0f3f8;--txt:#1a2030;--txt2:#5b6478;--line:#dde3ee}}
*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);padding:28px 14px;line-height:1.5}}
.wrap{{max-width:1200px;margin:0 auto}}h1{{font-size:22px;margin-bottom:4px}}.sub{{color:var(--txt2);font-size:13px;margin-bottom:18px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 18px;font-size:13px}}
.kpi b{{font-size:20px;display:block}}table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;border-radius:12px;overflow:hidden}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:12px;margin-bottom:24px}}
th{{background:var(--card2);text-align:left;padding:9px 10px;font-size:11px;text-transform:uppercase;color:var(--txt2);white-space:nowrap}}
td{{padding:9px 10px;border-top:1px solid var(--line);vertical-align:top}}td.num{{text-align:right;white-space:nowrap}}
.adname{{max-width:330px}}small{{color:var(--txt2)}}
.pill{{padding:2px 9px;border-radius:10px;font-size:11px;font-weight:700;white-space:nowrap}}
h2{{font-size:16px;margin:18px 0 8px}}
.btn{{position:fixed;top:14px;right:14px;background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:18px;padding:6px 16px;cursor:pointer;font-size:12px;font-weight:600}}
</style></head><body>
<button class="btn" onclick="tt()">off/white</button><div class="wrap">
<h1>🔎 Varredura Diária — Régua v2</h1>
<p class="sub">{HOJE} · leitura para decisão (o agente não pausa nada) · Meta API: {'ok' if api_ok else 'indisponível — sem frequência'} · janela veterano: {W1_INI} → {W1_FIM}</p>
<div class="kpis">
<div class="kpi" style="border-left:3px solid #f87171"><b>{n['PAUSAR']}</b>🔴 pausar</div>
<div class="kpi" style="border-left:3px solid #fb923c"><b>{n['AUDITAR']}</b>🟠 auditar piso</div>
<div class="kpi" style="border-left:3px solid #fbbf24"><b>{n['ATENCAO']}</b>⚠ 1ª janela</div>
<div class="kpi" style="border-left:3px solid #fbbf24"><b>{n['AMARELO']}</b>🟡 amarelo</div>
<div class="kpi" style="border-left:3px solid #34d399"><b>{n['VERDE']}</b>🟢 verde</div>
<div class="kpi"><b>{n['AMOSTRA'] + n['AGUARDANDO']}</b>⏳ sem amostra</div>
<div class="kpi" style="border-left:3px solid #a78bfa"><b>{out['fadiga']}</b>😴 fadiga ≥{FREQ_FADIGA}</div>
</div>
<h2>Resumo por célula (janela 7d)</h2>
<div class="tw"><table><tr><th>Célula</th><th>Gasto 7d</th><th>Custo/resultado</th><th>Teto</th><th>Chegada CRM</th><th>Métrica</th></tr>
{''.join(cel_linha(k, c) for k, c in sorted(celulas.items()))}</table></div>
<h2>Anúncios ({len(leitura)})</h2>
<div class="tw"><table><tr><th>Status</th><th>Anúncio / campanha</th><th>Célula</th><th>Idade</th><th>Custo</th><th>Teto</th><th>Gasto*</th><th>Freq 7d</th><th>Leitura / ação recomendada</th></tr>
{''.join(linha(x) for x in leitura)}</table></div>
<p class="sub">*Gasto: acumulado desde o início (novos) ou janela 7d (veteranos). Bandas oficiais 2026-08-07 —
cadastro medido em lead CRM (teto convertido pela taxa de chegada). Spec: REGUA-OTIMIZACAO-CRIATIVOS-v2.md</p>
</div><script>
const p=new URLSearchParams(location.search).get('theme'),s=localStorage.getItem('theme');
if((p||s)==='light')document.documentElement.setAttribute('data-theme','light');
function tt(){{const h=document.documentElement,l=h.getAttribute('data-theme')==='light';
if(l){{h.removeAttribute('data-theme');localStorage.setItem('theme','dark')}}else{{h.setAttribute('data-theme','light');localStorage.setItem('theme','light')}}}}
</script></body></html>"""
(ROOT / 'varredura-diaria.html').write_text(html, encoding='utf-8')

print(f"varredura OK: {len(leitura)} anuncios | PAUSAR={n['PAUSAR']} AUDITAR={n['AUDITAR']} "
      f"ATENCAO={n['ATENCAO']} VERDE={n['VERDE']} fadiga={out['fadiga']} | API Meta: {api_ok}")
for x in leitura:
    if x['status'] in ('PAUSAR', 'AUDITAR', 'ATENCAO'):
        print(f"  [{x['status']}] {x['conta']} {x['celula']} | {x['ad'][:50]} | {x['acao']}")

# ---- 5. Git push (publica no GitHub Pages, como o placar) ----------------
if '--no-push' not in sys.argv:
    subprocess.run(['git', '-C', str(ROOT), 'add', 'data/varredura.json', 'varredura-diaria.html'],
                   capture_output=True, text=True)
    r = subprocess.run(['git', '-C', str(ROOT), 'commit', '-m', f'chore: varredura {HOJE}'],
                       capture_output=True, text=True)
    if 'nothing to commit' not in r.stdout:
        r = subprocess.run(['git', '-C', str(ROOT), 'push'], capture_output=True, text=True)
        print('Push OK' if r.returncode == 0 else f'Erro no push: {r.stderr[:200]}')
