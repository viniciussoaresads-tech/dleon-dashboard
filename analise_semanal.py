# -*- coding: utf-8 -*-
"""Analise Semanal — decisao de escala (camada 2 da Regua v2) -> ClickUp

Le data/placar_dataset.json (series diarias por anuncio, coorte por data do lead)
e escreve a analise de ESCALA como comentario na tarefa do ritual semanal:

  ClickUp 86ajeuvtn — "[SEMANAL] Placar de Otimizacao — custo por comparecimento"

O que responde (janela SOP D-21..D-8, fechada, excluindo 7d de maturacao):
  - Quem FORMOU (2 janelas verdes seguidas) -> migrar pro motor / +20%
  - Quem escala, quem corta (decisor: custo/comparecimento, criterio v1)
  - Custo por agendamento por celula vs alvo (regua v2) + realocacao sugerida
  - Saude do pipeline criativo (testes novos na ultima semana)
  - Taxa de chegada Meta->CRM da semana por celula

Chamado pelo varredura_diaria.py as segundas (apos placar). Somente leitura.
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
TASK_ID = '86ajeuvtn'
CENTRAL = 'https://viniciussoaresads-tech.github.io/dleon-dashboard/placar-semanal.html'

ACCOUNTS = {'926172801304741': ('Lentes', 'lentes'),
            '1543033206555581': ('Protocolo', 'protocolo'),
            '1137133676785413': ('HOF', 'hof')}

# Alvos de custo por AGENDAMENTO por celula (regua v2, escala CRM)
AGEND_ALVO = {'lentes|mensagem': (219, 292), 'lentes|cadastro': (219, 292),
              'protocolo|mensagem': (105, 147), 'protocolo|cadastro': (57, 125),
              'hof|mensagem': (94, 125), 'hof|cadastro': (38, 50)}
# Decisor v1 (custo por comparecimento, calibrado Lentes — direcional nas demais)
COMP_VERDE, COMP_TETO = 700, 1500


def funil_of(camp):
    c = (camp or '').upper()
    if re.search(r'CADASTRO|TYPEBO|QUIZ', c):
        return 'cadastro'
    if re.search(r'MSG|WHAT|DIRECT|MENSAGEM', c):
        return 'mensagem'
    return 'outros'


def brl(n):
    return 'R$ ' + f'{n:,.0f}'.replace(',', '.')


DS = json.loads((ROOT / 'data' / 'placar_dataset.json').read_text(encoding='utf-8'))
BASE = date.fromisoformat(DS['base'])

# Janelas SOP: A = atual (D-21..D-8) | B = anterior (D-35..D-22)
A0, A1 = HOJE - timedelta(days=21), HOJE - timedelta(days=8)
B0, B1 = HOJE - timedelta(days=35), HOJE - timedelta(days=22)
S0, S1 = HOJE - timedelta(days=7), HOJE - timedelta(days=1)   # semana p/ chegada/pipeline


def agg(ad, d0, d1):
    o0, o1 = (d0 - BASE).days, (d1 - BASE).days
    t = [0.0, 0, 0, 0, 0, 0]
    for k, v in ad['d'].items():
        if o0 <= int(k) <= o1:
            for i in range(6):
                t[i] += v[i]
    return t  # spend, conv, leads, agendou, compareceu, vendas


def semaforo_comp(sp, comp, leads, idade):
    if idade < 21 and leads < 150:
        return 'aprendizado', None
    custo = sp / comp if comp and sp else None
    if custo is not None and custo < COMP_VERDE:
        return 'verde', custo
    if custo is not None and custo <= COMP_TETO:
        return 'amarelo', custo
    if custo is not None:
        return 'vermelho', custo
    if sp > 1500 and comp == 0:
        return 'vermelho', None
    return 'amarelo', None


rows, cel, novos_semana = [], {}, {}
for ad in DS['ads']:
    if ad['acc'] not in ACCOUNTS:
        continue
    conta, prod = ACCOUNTS[ad['acc']]
    canal = funil_of(ad['camp'])
    pg = date.fromisoformat(ad['pg']) if ad.get('pg') else None
    idade = (HOJE - pg).days if pg else 0
    if pg and pg >= S0:
        novos_semana[conta] = novos_semana.get(conta, 0) + 1

    spA, cvA, leA, agA, cpA, veA = agg(ad, A0, A1)
    if canal != 'outros' and spA > 0:
        k = f'{prod}|{canal}'
        c = cel.setdefault(k, {'sp': 0, 'ag': 0, 'cp': 0, 'le': 0, 'cv': 0,
                               'spS': 0, 'leS': 0, 'cvS': 0})
        c['sp'] += spA; c['ag'] += agA; c['cp'] += cpA; c['le'] += leA; c['cv'] += cvA
        spS, cvS, leS, *_ = agg(ad, S0, S1)
        c['spS'] += spS; c['cvS'] += cvS; c['leS'] += leS

    if spA < 300 or canal == 'outros':
        continue
    corA, custoA = semaforo_comp(spA, cpA, leA, idade)
    spB, _, leB, agB, cpB, veB = agg(ad, B0, B1)
    corB, _ = semaforo_comp(spB, cpB, leB, idade - 14) if spB >= 300 else ('sem', None)
    rows.append({'ad': (ad['ad'] or '(sem nome)')[:55], 'cr': ad.get('cr'), 'conta': conta,
                 'cel': f'{prod}|{canal}', 'idade': idade, 'sp': spA, 'leads': leA,
                 'ag': agA, 'cp': cpA, 'vd': veA, 'corA': corA, 'corB': corB,
                 'custo': custoA,
                 'cagend': spA / agA if agA else None})

# ---- Compoe a analise ----------------------------------------------------
L = [f'📊 ANÁLISE SEMANAL DE ESCALA — {HOJE} (Régua v2 · janela {A0} → {A1})', '']

formados = [r for r in rows if r['corA'] == 'verde' and r['corB'] == 'verde'
            and r['cp'] >= 3 and r['sp'] >= 500]
if formados:
    L.append(f'🎓 FORMARAM (2 janelas verdes) — migrar pro motor / escalar +20% ({len(formados)})')
    for r in sorted(formados, key=lambda x: x['custo'] or 9e9)[:5]:
        L.append(f"• {r['ad']} [{r['cel']}] — {brl(r['custo'])}/comparecimento · "
                 f"{r['cp']} comp · {r['vd']} vendas parciais · gasto {brl(r['sp'])}")
    L.append('')

escalar = [r for r in rows if r['corA'] == 'verde' and r['corB'] != 'verde']
if escalar:
    L.append(f'🟢 1ª JANELA VERDE — escalar +20% moderado e revalidar ({len(escalar)})')
    for r in sorted(escalar, key=lambda x: x['custo'] or 9e9)[:5]:
        L.append(f"• {r['ad']} [{r['cel']}] — {brl(r['custo'])}/comp · gasto {brl(r['sp'])}")
    L.append('')

cortar2 = [r for r in rows if r['corA'] == 'vermelho' and r['corB'] == 'vermelho']
cortar1 = [r for r in rows if r['corA'] == 'vermelho' and r['corB'] != 'vermelho']
if cortar2:
    L.append(f'🔴 2 JANELAS VERMELHAS — pausar ({len(cortar2)})')
    for r in sorted(cortar2, key=lambda x: -x['sp'])[:5]:
        cc = brl(r['custo']) + '/comp' if r['custo'] else 'ZERO comparecimentos'
        L.append(f"• {r['ad']} [{r['cel']}] — {cc} · gasto {brl(r['sp'])}")
    L.append('')
if cortar1:
    L.append(f'🟠 1ª JANELA VERMELHA — cortar 50% ({len(cortar1)})')
    for r in sorted(cortar1, key=lambda x: -x['sp'])[:5]:
        cc = brl(r['custo']) + '/comp' if r['custo'] else 'ZERO comparecimentos'
        L.append(f"• {r['ad']} [{r['cel']}] — {cc} · gasto {brl(r['sp'])}")
    L.append('')

# Celulas: custo/agendamento vs alvo + realocacao
L.append('📐 CÉLULAS — custo por agendamento (janela SOP) vs alvo da régua:')
rank = []
for k, c in sorted(cel.items()):
    if k not in AGEND_ALVO or not c['ag']:
        continue
    ca = c['sp'] / c['ag']
    alvo, teto = AGEND_ALVO[k]
    tag = '🟢' if ca <= alvo else ('🟡' if ca <= teto else '🔴')
    cheg = f" · chegada semana {round(100*c['leS']/c['cvS'],1)}%" if k.endswith('mensagem') and c['cvS'] else ''
    L.append(f"• {tag} {k}: {brl(ca)}/agend (alvo {brl(alvo)} · teto {brl(teto)}) · "
             f"{c['ag']} agend · gasto {brl(c['sp'])}{cheg}")
    rank.append((k, ca / alvo))
if len(rank) >= 2:
    rank.sort(key=lambda x: x[1])
    melhor, pior = rank[0], rank[-1]
    if pior[1] > 1.2 and melhor[1] < 1.0:
        L.append(f"→ Realocação sugerida: deslocar ~10-15% do budget de {pior[0]} "
                 f"(a {pior[1]:.1f}x do alvo) para {melhor[0]} (a {melhor[1]:.1f}x do alvo)")
L.append('')

# Pipeline criativo
tot_novos = sum(novos_semana.values())
pipe = ' · '.join(f'{k}: {v}' for k, v in sorted(novos_semana.items())) or 'nenhum'
flag = ' ⚠ abaixo do ritmo (meta: 3-5/semana em Lentes)' if novos_semana.get('Lentes', 0) < 3 else ''
L.append(f'🎬 PIPELINE CRIATIVO — testes novos na semana: {tot_novos} ({pipe}){flag}')

apr = sum(1 for r in rows if r['corA'] == 'aprendizado')
L.append(f'Panorama da janela: {len(rows)} anúncios julgáveis · {apr} em aprendizado (não julgar)')
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
        print(f'analise semanal postada no ClickUp ({TASK_ID}): ok')
except Exception as e:
    print(f'analise semanal gerada; falha ao postar: {e}', file=sys.stderr)
    print(texto)
    sys.exit(1)
