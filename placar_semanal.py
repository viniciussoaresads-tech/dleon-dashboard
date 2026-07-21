# -*- coding: utf-8 -*-
"""Placar de Otimizacao — Datalake Uno x Meta (SOP Otimizacao por Receita v1.0)

Gera dois arquivos:
  - data/placar_dataset.json  -> series DIARIAS por anuncio (spend, conversas,
    leads, agendou, compareceu, vendas) das 3 contas ativas. O frontend
    (placar-semanal.html) recalcula o placar para qualquer intervalo de data
    e conta escolhidos (filtro estilo Meta) — decisor = custo por comparecimento.
  - data/placar.json          -> snapshot legado da conta Lentes na janela SOP
    (D-21 a D-7), para compatibilidade.

Decisor: custo por comparecimento (lead matura ~9d ate a consulta).
Agendado: toda segunda 08:00 (Task Scheduler 'DLeon-Placar-Semanal').
"""
import psycopg2
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
RETAIN = 130  # dias retidos no dataset (cobre presets ate 90d + mes passado)

# Contas ativas da D'Leon no datalake (produto inferido pelas campanhas)
ACCOUNTS = {
    '926172801304741': 'Lentes',
    '1543033206555581': 'Protocolo / Implante',
    '1137133676785413': 'HOF / Homem das Lentes',
}
LENTES = '926172801304741'

today = date.today()
BASE = today - timedelta(days=RETAIN)  # offset 0 do dataset


def off(d):
    return (d - BASE).days


def criativo_key(ad):
    """Codigo do criativo base p/ consolidacao: [NNN] quando existir."""
    if not ad:
        return '(sem nome)'
    m = re.search(r'\[(\d{2,3})\]', ad)
    if m:
        return '[' + m.group(1) + ']'
    base = re.sub(r'\.(mp4|mov|jpg|png|jpeg)', '', ad, flags=re.I)
    base = re.sub(r'\s*C[oó]pia.*$', '', base, flags=re.I).strip()
    return base[:40] or '(sem nome)'


conn = psycopg2.connect(
    host='uno-datalake-cluster-1.cp74abrqalt6.sa-east-1.redshift.amazonaws.com',
    port=5439, dbname='uno', user='com8053_reader',
    password='4rCENOaB9c92m1I0cX5AHKmdMd3', sslmode='require', connect_timeout=60
)
cur = conn.cursor()
accs = tuple(ACCOUNTS.keys())

# --- Metadados por anuncio (nome, campanha, primeiro/ultimo gasto global) ---
cur.execute("""
SELECT source_id, account_id, MAX(ad_name), MAX(ad_campaign_name),
       MIN(CASE WHEN total_spent>0 THEN date END),
       MAX(CASE WHEN total_spent>0 THEN date END)
FROM com8053.facebook_campaign_data
WHERE schema='clinica_dleon' AND account_id IN %(a)s
GROUP BY source_id, account_id
""", {'a': accs})
meta = {}
for sid, acc, ad, camp, pg, ug in cur.fetchall():
    meta[sid] = {'id': str(sid), 'acc': acc, 'ad': ad, 'camp': camp,
                 'pg': str(pg) if pg else None, 'ug': str(ug) if ug else None}

# --- Serie diaria de midia (gasto + conversas) na janela retida ---
cur.execute("""
SELECT source_id, date, ROUND(SUM(total_spent)::numeric,0), SUM(started_messages)
FROM com8053.facebook_campaign_data
WHERE schema='clinica_dleon' AND account_id IN %(a)s AND date >= %(b)s
GROUP BY source_id, date
""", {'a': accs, 'b': str(BASE)})
days = {}  # sid -> {offset: [spend, conversas, leads, agendou, compareceu, vendas]}
for sid, d, sp, conv in cur.fetchall():
    days.setdefault(sid, {})[off(d)] = [int(sp or 0), int(conv or 0), 0, 0, 0, 0]

# --- Serie diaria de leads (coorte por created_at, join por source_id) ---
cur.execute("""
SELECT facebook_source_id, created_at::date,
       COUNT(*), COUNT(customer_appointment_date),
       COUNT(CASE WHEN customer_appointment_status_id=5 THEN 1 END),
       COUNT(CASE WHEN converted_at IS NOT NULL THEN 1 END)
FROM com8053.deals
WHERE schema='clinica_dleon' AND created_at::date >= %(b)s
  AND facebook_source_id IS NOT NULL
GROUP BY 1,2
""", {'b': str(BASE)})
for sid, d, le, ag, cp, ve in cur.fetchall():
    if sid not in meta:
        continue  # lead de anuncio fora das 3 contas
    o = off(d)
    row = days.setdefault(sid, {}).get(o)
    if row is None:
        row = [0, 0, 0, 0, 0, 0]
        days[sid][o] = row
    row[2] += int(le); row[3] += int(ag); row[4] += int(cp); row[5] += int(ve)

# --- Monta lista de anuncios do dataset ---
ads = []
for sid, dd in days.items():
    m = meta.get(sid)
    if not m:
        continue
    if not any(r[0] > 0 or r[2] > 0 for r in dd.values()):
        continue  # sem gasto e sem lead na janela retida
    ads.append({
        'id': m['id'], 'acc': m['acc'], 'ad': m['ad'], 'camp': m['camp'],
        'cr': criativo_key(m['ad']), 'pg': m['pg'], 'ug': m['ug'],
        'd': {str(k): v for k, v in sorted(dd.items())},
    })

dataset = {
    'gerado_em': str(today), 'base': str(BASE), 'retain': RETAIN,
    'accounts': [{'id': k, 'nome': v} for k, v in ACCOUNTS.items()],
    'ads': ads,
}
(ROOT / 'data' / 'placar_dataset.json').write_text(
    json.dumps(dataset, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

# --- Snapshot legado (Lentes, janela SOP D-21 a D-7) p/ compatibilidade ---
D7, D21 = today - timedelta(days=7), today - timedelta(days=21)
o7, o21 = off(D7), off(D21)


def semaforo(sp_jan, compareceu, leads, idade_dias, cobertura, conv_jan):
    em_graca = idade_dias < 21
    custo_comp = round(sp_jan / compareceu, 0) if compareceu > 0 and sp_jan > 0 else None
    if em_graca and leads < 150:
        return 'graca', f'EM APRENDIZADO ({idade_dias}d / {leads} leads) — nao julgar', custo_comp
    if cobertura is not None and cobertura < 40 and conv_jan >= 50:
        return 'tracking', f'COBERTURA {cobertura}% — corrigir tracking antes de julgar', custo_comp
    if custo_comp is not None and custo_comp < 700:
        return 'verde', 'ESCALAR +20%', custo_comp
    if custo_comp is not None and custo_comp <= 1500:
        return 'amarelo', 'MANTER', custo_comp
    if custo_comp is not None:
        return 'vermelho', 'CORTAR 50% (pausar se 2a semana vermelha)', custo_comp
    if sp_jan > 1500 and compareceu == 0:
        return 'vermelho', 'ZERO comparecimentos com gasto relevante — cortar/pausar', custo_comp
    if sp_jan > 0:
        return 'amarelo', 'Amostra insuficiente — observar', custo_comp
    return 'graca', 'Gasto recente apenas (fora da janela) — aguardar maturacao', custo_comp


rows = []
for a in ads:
    if a['acc'] != LENTES:
        continue
    sp_jan = conv_jan = leads = agendou = compareceu = vendas = sp_7d = 0
    for k, v in a['d'].items():
        o = int(k)
        if o21 <= o < o7:
            sp_jan += v[0]; conv_jan += v[1]; leads += v[2]
            agendou += v[3]; compareceu += v[4]; vendas += v[5]
        if o >= o7:
            sp_7d += v[0]
    if not (sp_7d > 50 or sp_jan > 300):
        continue
    pg = date.fromisoformat(a['pg']) if a['pg'] else None
    idade = (today - pg).days if pg else 0
    cobertura = round(100.0 * leads / conv_jan, 1) if conv_jan > 0 else None
    cpl = round(sp_jan / leads, 1) if leads > 0 and sp_jan > 0 else None
    cor, acao, custo_comp = semaforo(sp_jan, compareceu, leads, idade, cobertura, conv_jan)
    rows.append({
        'id': a['id'], 'ad': a['ad'], 'camp': a['camp'],
        'gasto_janela': sp_jan, 'gasto_7d': sp_7d, 'leads': leads,
        'pct_agendou': round(100.0 * agendou / leads, 1) if leads else None,
        'pct_compareceu': round(100.0 * compareceu / agendou, 1) if agendou else None,
        'compareceu': compareceu, 'vendas': vendas,
        'custo_comp': custo_comp, 'cpl': cpl, 'cobertura': cobertura,
        'idade_dias': idade, 'cor': cor, 'acao': acao,
    })

# regressao de cobertura (7d vs 7d anterior) — conta Lentes
cur.execute("""
SELECT
  SUM(CASE WHEN created_at::date >= %(d7)s THEN 1 ELSE 0 END),
  SUM(CASE WHEN created_at::date >= %(d7)s
           AND COALESCE(facebook_source_id, facebook_wacl_id) IS NOT NULL THEN 1 ELSE 0 END),
  SUM(CASE WHEN created_at::date >= %(d14)s AND created_at::date < %(d7)s THEN 1 ELSE 0 END),
  SUM(CASE WHEN created_at::date >= %(d14)s AND created_at::date < %(d7)s
           AND COALESCE(facebook_source_id, facebook_wacl_id) IS NOT NULL THEN 1 ELSE 0 END)
FROM com8053.deals
WHERE schema='clinica_dleon' AND created_at::date >= %(d14)s
""", {'d7': str(D7), 'd14': str(today - timedelta(days=14))})
c = cur.fetchone()
conn.close()
cob_7d = round(100.0 * c[1] / c[0], 1) if c[0] else None
cob_prev = round(100.0 * c[3] / c[2], 1) if c[2] else None
alerta = None
if cob_7d is not None and cob_prev is not None and (cob_prev - cob_7d) > 10:
    alerta = f'REGRESSAO: cobertura caiu de {cob_prev}% para {cob_7d}% — investigar integracao'

legado = {
    'gerado_em': str(today),
    'janela': {'inicio': str(D21), 'fim': str(D7 - timedelta(days=1))},
    'resumo': {
        'ads_no_placar': len(rows),
        'gasto_janela': round(sum(x['gasto_janela'] for x in rows), 0),
        'comparecimentos': sum(x['compareceu'] for x in rows),
        'vendas_janela': sum(x['vendas'] for x in rows),
        'cobertura_7d': cob_7d, 'cobertura_semana_anterior': cob_prev,
        'alerta_cobertura': alerta,
    },
    'anuncios': rows,
}
(ROOT / 'data' / 'placar.json').write_text(
    json.dumps(legado, ensure_ascii=False), encoding='utf-8')

print(f"dataset OK: {len(ads)} anuncios | base {BASE} .. {today} | legado: {len(rows)} ads Lentes | cobertura 7d: {cob_7d}%")
if alerta:
    print("!!", alerta)

# --- Git push ---
subprocess.run(['git', '-C', str(ROOT), 'add', 'data/placar_dataset.json',
                'data/placar.json', 'placar-semanal.html'], capture_output=True, text=True)
r = subprocess.run(['git', '-C', str(ROOT), 'commit', '-m', f'chore: placar {today}'],
                   capture_output=True, text=True)
if 'nothing to commit' in r.stdout:
    print("Sem mudancas.")
    sys.exit(0)
r = subprocess.run(['git', '-C', str(ROOT), 'push'], capture_output=True, text=True)
print("Push OK" if r.returncode == 0 else f"Erro no push: {r.stderr}")
