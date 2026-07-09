# -*- coding: utf-8 -*-
"""Placar Semanal de Otimizacao — Conta Lentes (SOP Otimizacao por Receita v1.0)
Janela de decisao: leads criados entre D-21 e D-7 (14 dias fechados, excluindo
os 7 dias mais recentes — lead matura em ~9 dias ate a consulta).
Gera data/placar.json e publica no GitHub Pages (placar-semanal.html).
Agendado: toda segunda 08:00 (Task Scheduler 'DLeon-Placar-Semanal').
"""
import psycopg2
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
ACCOUNT = '926172801304741'  # 01 ANUNCIOS D'LEON - LENTES

today = date.today()
D7 = today - timedelta(days=7)
D21 = today - timedelta(days=21)

conn = psycopg2.connect(
    host='uno-datalake-cluster-1.cp74abrqalt6.sa-east-1.redshift.amazonaws.com',
    port=5439, dbname='uno', user='com8053_reader',
    password='4rCENOaB9c92m1I0cX5AHKmdMd3', sslmode='require', connect_timeout=30
)
cur = conn.cursor()

cur.execute("""
WITH fb AS (
    SELECT source_id,
        MAX(ad_name) AS ad, MAX(ad_campaign_name) AS camp,
        SUM(CASE WHEN date >= %(d21)s AND date < %(d7)s THEN total_spent ELSE 0 END) AS spend_janela,
        SUM(CASE WHEN date >= %(d7)s THEN total_spent ELSE 0 END) AS spend_7d,
        SUM(CASE WHEN date >= %(d21)s AND date < %(d7)s THEN started_messages ELSE 0 END) AS conversas_janela,
        MIN(CASE WHEN total_spent > 0 THEN date END) AS primeiro_gasto,
        MAX(CASE WHEN total_spent > 0 THEN date END) AS ultimo_gasto
    FROM com8053.facebook_campaign_data
    WHERE schema='clinica_dleon' AND account_id=%(acc)s
    GROUP BY source_id
),
leads AS (
    SELECT facebook_source_id AS src,
        COUNT(*) AS leads,
        COUNT(customer_appointment_date) AS agendou,
        COUNT(CASE WHEN customer_appointment_status_id = 5 THEN 1 END) AS compareceu,
        COUNT(CASE WHEN converted_at IS NOT NULL THEN 1 END) AS vendas
    FROM com8053.deals
    WHERE schema='clinica_dleon'
      AND created_at::date >= %(d21)s AND created_at::date < %(d7)s
    GROUP BY 1
)
SELECT fb.source_id, fb.ad, fb.camp,
    ROUND(fb.spend_janela::numeric, 0), ROUND(fb.spend_7d::numeric, 0),
    fb.conversas_janela,
    COALESCE(l.leads, 0), COALESCE(l.agendou, 0), COALESCE(l.compareceu, 0), COALESCE(l.vendas, 0),
    fb.primeiro_gasto, fb.ultimo_gasto
FROM fb
LEFT JOIN leads l ON l.src = fb.source_id
WHERE fb.spend_7d > 50 OR fb.spend_janela > 300
ORDER BY fb.spend_janela + fb.spend_7d DESC
""", {'d21': str(D21), 'd7': str(D7), 'acc': ACCOUNT})

rows = []
for r in cur.fetchall():
    (sid, ad, camp, sp_jan, sp_7d, conv, leads, agendou, compareceu, vendas,
     primeiro, ultimo) = r
    sp_jan = float(sp_jan or 0); sp_7d = float(sp_7d or 0)
    leads = int(leads); agendou = int(agendou); compareceu = int(compareceu)
    conv = int(conv or 0)
    idade_dias = (today - primeiro).days if primeiro else 0
    em_graca = idade_dias < 21
    cobertura = round(100.0 * leads / conv, 1) if conv > 0 else None
    custo_comp = round(sp_jan / compareceu, 0) if compareceu > 0 and sp_jan > 0 else None
    pct_agendou = round(100.0 * agendou / leads, 1) if leads > 0 else None
    pct_comp = round(100.0 * compareceu / agendou, 1) if agendou > 0 else None
    cpl = round(sp_jan / leads, 1) if leads > 0 and sp_jan > 0 else None

    # Semaforo (regras do SOP)
    if em_graca and (leads < 150):
        cor, acao = 'graca', f'EM GRACA ({idade_dias}d / {leads} leads) — nao julgar'
    elif cobertura is not None and cobertura < 40 and conv >= 50:
        cor, acao = 'tracking', f'COBERTURA {cobertura}% — corrigir tracking antes de julgar'
    elif custo_comp is not None and custo_comp < 700:
        cor, acao = 'verde', 'ESCALAR +20%'
    elif custo_comp is not None and custo_comp <= 1500:
        cor, acao = 'amarelo', 'MANTER'
    elif custo_comp is not None:
        cor, acao = 'vermelho', 'CORTAR 50% (pausar se 2a semana vermelha)'
    elif sp_jan > 1500 and compareceu == 0:
        cor, acao = 'vermelho', 'ZERO comparecimentos com gasto relevante — cortar/pausar'
    elif sp_jan > 0:
        cor, acao = 'amarelo', 'Amostra insuficiente — observar'
    else:
        cor, acao = 'graca', 'Gasto recente apenas (fora da janela) — aguardar maturacao'

    rows.append({
        'id': str(sid), 'ad': ad, 'camp': camp,
        'gasto_janela': sp_jan, 'gasto_7d': sp_7d,
        'leads': leads, 'pct_agendou': pct_agendou, 'pct_compareceu': pct_comp,
        'compareceu': compareceu, 'vendas': int(vendas),
        'custo_comp': custo_comp, 'cpl': cpl, 'cobertura': cobertura,
        'idade_dias': idade_dias, 'cor': cor, 'acao': acao,
    })

# Resumo da conta + regressao de cobertura (ultimos 7 dias vs 7 anteriores)
cur.execute("""
SELECT
    SUM(CASE WHEN created_at::date >= %(d7)s THEN 1 ELSE 0 END) AS leads_7d,
    SUM(CASE WHEN created_at::date >= %(d7)s
             AND COALESCE(facebook_source_id, facebook_wacl_id) IS NOT NULL THEN 1 ELSE 0 END) AS lastro_7d,
    SUM(CASE WHEN created_at::date >= %(d14)s AND created_at::date < %(d7)s THEN 1 ELSE 0 END) AS leads_prev,
    SUM(CASE WHEN created_at::date >= %(d14)s AND created_at::date < %(d7)s
             AND COALESCE(facebook_source_id, facebook_wacl_id) IS NOT NULL THEN 1 ELSE 0 END) AS lastro_prev
FROM com8053.deals
WHERE schema='clinica_dleon' AND created_at::date >= %(d14)s
""", {'d7': str(D7), 'd14': str(today - timedelta(days=14))})
c = cur.fetchone()
cob_7d = round(100.0 * c[1] / c[0], 1) if c[0] else None
cob_prev = round(100.0 * c[3] / c[2], 1) if c[2] else None
conn.close()

alerta_cobertura = None
if cob_7d is not None and cob_prev is not None and (cob_prev - cob_7d) > 10:
    alerta_cobertura = f'REGRESSAO: cobertura caiu de {cob_prev}% para {cob_7d}% — investigar integracao'

out = {
    'gerado_em': str(today),
    'janela': {'inicio': str(D21), 'fim': str(D7 - timedelta(days=1))},
    'resumo': {
        'ads_no_placar': len(rows),
        'gasto_janela': round(sum(x['gasto_janela'] for x in rows), 0),
        'comparecimentos': sum(x['compareceu'] for x in rows),
        'vendas_janela': sum(x['vendas'] for x in rows),
        'cobertura_7d': cob_7d, 'cobertura_semana_anterior': cob_prev,
        'alerta_cobertura': alerta_cobertura,
    },
    'anuncios': rows,
}

placar_file = ROOT / 'data' / 'placar.json'
with open(placar_file, 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False)
print(f"placar OK: {len(rows)} anuncios | janela {D21} a {D7 - timedelta(days=1)} | cobertura 7d: {cob_7d}%")
if alerta_cobertura:
    print("!!", alerta_cobertura)

# Git push
subprocess.run(['git', '-C', str(ROOT), 'add', 'data/placar.json'], capture_output=True, text=True)
r = subprocess.run(['git', '-C', str(ROOT), 'commit', '-m', f'chore: placar semanal {today}'],
                   capture_output=True, text=True)
if 'nothing to commit' in r.stdout:
    print("Sem mudancas.")
    sys.exit(0)
r = subprocess.run(['git', '-C', str(ROOT), 'push'], capture_output=True, text=True)
print("Push OK" if r.returncode == 0 else f"Erro no push: {r.stderr}")
