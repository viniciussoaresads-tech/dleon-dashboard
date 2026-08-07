# -*- coding: utf-8 -*-
"""Analise Mensal — fechamento de safra (camada 3 da Regua v2) -> ClickUp

Le data/placar_dataset.json (coorte por data de criacao do lead) e escreve o
fechamento de safra como comentario na tarefa do ritual mensal:

  ClickUp 86ajeuvug — "[MENSAL] Validacao por Receita ERP + fadiga de campeoes"

O que responde:
  - Safra M-2 (FECHADA, >35d de maturacao): hall da fama por custo/venda,
    kills definitivos (gasto alto, zero resultado), celulas vs alvo
  - Safra M-1 (PARCIAL, ~5-35d): leitura preliminar por comparecimento
  - Lembrete de recalibragem trimestral dos tetos (R$/lead via pipeline kpi-2026)

Alvos de custo/venda (OKR implicito): Lentes R$560 · Protocolo R$640 · corte 1,5x.
HOF valida por agendamento (volume de venda baixo).

Roda no primeiro dia util >= dia 5 (marcador data/analise_mensal_last.txt).
Chamado pelo varredura_diaria.py. Somente leitura.
"""
import json
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).parent
HOJE = date.today()
TASK_ID = '86ajeuvug'
CENTRAL = 'https://viniciussoaresads-tech.github.io/dleon-dashboard/placar-semanal.html'

ACCOUNTS = {'926172801304741': ('Lentes', 'lentes'),
            '1543033206555581': ('Protocolo', 'protocolo'),
            '1137133676785413': ('HOF', 'hof')}
VENDA_ALVO = {'lentes': 560, 'protocolo': 640}   # corte em 1,5x


def funil_of(camp):
    c = (camp or '').upper()
    if re.search(r'CADASTRO|TYPEBO|QUIZ', c):
        return 'cadastro'
    if re.search(r'MSG|WHAT|DIRECT|MENSAGEM', c):
        return 'mensagem'
    return 'outros'


def brl(n):
    return 'R$ ' + f'{n:,.0f}'.replace(',', '.')


def mes_str(d):
    return f'{d.year}-{d.month:02d}'


DS = json.loads((ROOT / 'data' / 'placar_dataset.json').read_text(encoding='utf-8'))
BASE = date.fromisoformat(DS['base'])

m1_fim = HOJE.replace(day=1) - timedelta(days=1)          # ultimo dia de M-1
m1_ini = m1_fim.replace(day=1)
m2_fim = m1_ini - timedelta(days=1)
m2_ini = m2_fim.replace(day=1)
M1, M2 = mes_str(m1_ini), mes_str(m2_ini)


def agg(ad, d0, d1):
    o0, o1 = (max(d0, BASE) - BASE).days, (d1 - BASE).days
    t = [0.0, 0, 0, 0, 0, 0]
    for k, v in ad['d'].items():
        if o0 <= int(k) <= o1:
            for i in range(6):
                t[i] += v[i]
    return t  # spend, conv, leads, agendou, compareceu, vendas


def safra(d0, d1):
    """Agrega a safra (coorte de leads criados em d0..d1) por anuncio e celula."""
    ads_out, cel = [], {}
    for ad in DS['ads']:
        if ad['acc'] not in ACCOUNTS:
            continue
        conta, prod = ACCOUNTS[ad['acc']]
        canal = funil_of(ad['camp'])
        if canal == 'outros':
            continue
        sp, cv, le, ag, cp, vd = agg(ad, d0, d1)
        if sp <= 0 and le <= 0:
            continue
        k = f'{prod}|{canal}'
        c = cel.setdefault(k, {'sp': 0, 'le': 0, 'ag': 0, 'cp': 0, 'vd': 0})
        c['sp'] += sp; c['le'] += le; c['ag'] += ag; c['cp'] += cp; c['vd'] += vd
        ads_out.append({'ad': (ad['ad'] or '(sem nome)')[:55], 'conta': conta, 'prod': prod,
                        'cel': k, 'sp': sp, 'le': le, 'ag': ag, 'cp': cp, 'vd': vd})
    return ads_out, cel


L = [f'🏁 FECHAMENTO DE SAFRA — gerado {HOJE} (Régua v2, camada 3)', '']

# ---- Safra M-2 (fechada) -------------------------------------------------
if m2_ini >= BASE:
    ads2, cel2 = safra(m2_ini, m2_fim)
    L.append(f'📦 SAFRA {M2} — FECHADA (>35d de maturação, veredito definitivo)')
    hall = sorted([a for a in ads2 if a['vd'] >= 2 and a['sp'] >= 500],
                  key=lambda a: a['sp'] / a['vd'])[:5]
    if hall:
        L.append('🏆 HALL DA FAMA (custo/venda da safra — modelar novos criativos nesses):')
        for a in hall:
            alvo = VENDA_ALVO.get(a['prod'])
            tag = ' 🟢' if alvo and a['sp'] / a['vd'] <= alvo else ''
            L.append(f"• {a['ad']} [{a['cel']}] — {brl(a['sp']/a['vd'])}/venda ({a['vd']} vendas · "
                     f"gasto {brl(a['sp'])}){tag}")
    kills = sorted([a for a in ads2 if a['sp'] >= 1000 and a['vd'] == 0 and a['cp'] == 0],
                   key=lambda a: -a['sp'])[:5]
    if kills:
        L.append('💀 KILL DEFINITIVO (safra fechada, gasto ≥R$1.000, zero comparecimento e venda):')
        for a in kills:
            L.append(f"• {a['ad']} [{a['cel']}] — {brl(a['sp'])} gastos · {a['le']} leads · 0 comp · 0 vendas")
    L.append('Células da safra ' + M2 + ' (vendas RASTREADAS por source_id — subconta ~2-3x '
             'vs visão com rateio; compare células e criativos entre si, não com o alvo absoluto):')
    for k, c in sorted(cel2.items()):
        cv = brl(c['sp'] / c['vd']) + '/venda rastreada' if c['vd'] else 'sem venda rastreada'
        L.append(f"• {k}: {brl(c['sp'])} → {c['le']} leads · {c['ag']} agend · {c['cp']} comp · "
                 f"{c['vd']} vendas · {cv}")
    L.append('')
else:
    L.append(f'📦 Safra {M2}: fora da janela retida do dataset (130d) — sem fechamento.')
    L.append('')

# ---- Safra M-1 (parcial) -------------------------------------------------
ads1, cel1 = safra(m1_ini, m1_fim)
idade_min = (HOJE - m1_fim).days
L.append(f'⏳ SAFRA {M1} — PARCIAL ({idade_min}-{(HOJE-m1_ini).days}d de maturação; '
         f'veredito de venda só no próximo fechamento)')
for k, c in sorted(cel1.items()):
    ca = brl(c['sp'] / c['ag']) + '/agend' if c['ag'] else 'sem agend'
    cc = brl(c['sp'] / c['cp']) + '/comp' if c['cp'] else 'sem comp'
    L.append(f"• {k}: {brl(c['sp'])} → {c['le']} leads · {c['ag']} agend ({ca}) · "
             f"{c['cp']} comp ({cc}) · {c['vd']} vendas parciais")
L.append('')
L.append('🔧 RECALIBRAGEM: rodar pipeline dleon-kpi-2026 p/ receita por célula (R$/lead) e '
         'revisar tetos da régua (obrigatório no fechamento de trimestre). '
         'Células em observação: lentes|cadastro e protocolo|mensagem (tetos generosos vs rastreado).')
L.append(f'Central: {CENTRAL}')
texto = '\n'.join(L)

# ---- Posta no ClickUp ----------------------------------------------------
if '--dry' in sys.argv:
    print(texto)
    sys.exit(0)
mcp = json.loads(Path(r'C:\Users\vinic\.mcp.json').read_text(encoding='utf-8'))
token = mcp['mcpServers']['agencia']['env']['CLICKUP_API_TOKEN']
req = urllib.request.Request(
    f'https://api.clickup.com/api/v2/task/{TASK_ID}/comment',
    data=json.dumps({'comment_text': texto, 'notify_all': False}).encode('utf-8'),
    headers={'Authorization': token, 'Content-Type': 'application/json'}, method='POST')
try:
    with urllib.request.urlopen(req, timeout=45) as resp:
        print(f'analise mensal postada no ClickUp ({TASK_ID}): ok')
    (ROOT / 'data' / 'analise_mensal_last.txt').write_text(mes_str(HOJE), encoding='utf-8')
except Exception as e:
    print(f'analise mensal gerada; falha ao postar: {e}', file=sys.stderr)
    print(texto)
    sys.exit(1)
