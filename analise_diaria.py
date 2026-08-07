# -*- coding: utf-8 -*-
"""Analise Diaria — interpreta a varredura e posta no ClickUp (ritual diario)

Le data/varredura.json (gerada pelo varredura_diaria.py), mantem historico
em data/varredura_hist.json e escreve a LEITURA INTERPRETADA como comentario
na tarefa do ritual diario de trafego:

  ClickUp 86ajeuvrf — "[DIARIO] Guard-rails de trafego" (lista Gestao de Trafego)

O que a analise responde:
  - Que decisao esta pendente e ha quantos dias (custo da inacao em R$)
  - O que mudou vs ontem (novos alertas, resolvidos, voltou pra banda)
  - Quais verdes estao prontos p/ validar escala
  - Flags de fadiga e de fluxo (taxa de chegada Meta->CRM)

Chamado pelo varredura_diaria.py (dias uteis 08h). Somente leitura + comentario.
"""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).parent
HOJE = str(date.today())
TASK_ID = '86ajeuvrf'
CENTRAL = 'https://viniciussoaresads-tech.github.io/dleon-dashboard/placar-semanal.html'
EXC = ('PAUSAR', 'AUDITAR', 'ATENCAO')
RETER_DIAS = 40

var = json.loads((ROOT / 'data' / 'varredura.json').read_text(encoding='utf-8'))
hist_path = ROOT / 'data' / 'varredura_hist.json'
hist = json.loads(hist_path.read_text(encoding='utf-8')) if hist_path.exists() else {}

# ---- 1. Registra hoje no historico --------------------------------------
hist[HOJE] = {a['sid']: {'s': a['status'], 'g': a.get('gasto_d1') or 0}
              for a in var.get('anuncios', [])}
for d in sorted(hist)[:-RETER_DIAS]:
    del hist[d]
hist_path.write_text(json.dumps(hist, ensure_ascii=False), encoding='utf-8')

dias = sorted(hist)
ontem = dias[-2] if len(dias) >= 2 else None
h_ontem = hist.get(ontem, {}) if ontem else {}
ads = {a['sid']: a for a in var.get('anuncios', [])}


def brl(n):
    return 'R$ ' + f'{n:,.0f}'.replace(',', '.')


def nome(a):
    return (a.get('ad') or '(sem nome)')[:55]


def dias_pendente(sid, status):
    """Dias consecutivos (incl. hoje) com o mesmo status + gasto no periodo."""
    n, gasto = 0, 0.0
    for d in reversed(dias):
        rec = hist.get(d, {}).get(sid)
        if rec and rec['s'] == status:
            n += 1
            gasto += rec.get('g') or 0
        else:
            break
    return n, gasto


# ---- 2. Blocos da analise ------------------------------------------------
L = [f'🔎 LEITURA DIÁRIA — {HOJE} (agente Régua v2)', '']

pausar = [a for a in var['anuncios'] if a['status'] == 'PAUSAR']
if pausar:
    L.append(f'🔴 DECISÃO PENDENTE — PAUSAR ({len(pausar)})')
    for a in pausar:
        nd, g = dias_pendente(a['sid'], 'PAUSAR')
        pend = f' · alerta há {nd} dia(s), ~{brl(g)} gastos desde então' if nd > 1 else ''
        L.append(f"• {nome(a)} [{a['celula']}{', NOVO' if a.get('novo') else ''}] — "
                 f"R$ {a['custo']:.2f} vs teto R$ {a['teto']:.2f}{pend}")
    L.append('')

auditar = [a for a in var['anuncios'] if a['status'] == 'AUDITAR']
if auditar:
    L.append(f'🟠 AUDITAR PISO ({len(auditar)}) — barato demais, conferir % agendamento antes de escalar')
    for a in auditar:
        L.append(f"• {nome(a)} [{a['celula']}] — R$ {a['custo']:.2f} (piso da célula)")
    L.append('')

atencao = [a for a in var['anuncios'] if a['status'] == 'ATENCAO']
if atencao:
    L.append(f'⚠️ 1ª JANELA ACIMA DO TETO ({len(atencao)}) — pausa se repetir na próxima')
    for a in atencao:
        L.append(f"• {nome(a)} [{a['celula']}] — R$ {a['custo']:.2f} vs teto R$ {a['teto']:.2f}")
    L.append('')

fadiga = [a for a in var['anuncios'] if (a.get('freq') or 0) >= 2.8]
if fadiga:
    tops = ' · '.join(f"{nome(a)} (freq {a['freq']:.1f})" for a in fadiga[:4])
    L.append(f'😴 FADIGA ({len(fadiga)}): {tops} → pedir criativo novo')
    L.append('')

fluxo = [(k, c) for k, c in var.get('celulas', {}).items()
         if c.get('chegada') is not None and c['chegada'] < 55]
for k, c in fluxo:
    L.append(f"⚙️ FLUXO: {k} com chegada Meta→CRM de {c['chegada']}% (mín. 55%) — "
             f'investigar bot/API antes de julgar criativo')
if fluxo:
    L.append('')

verdes = sorted([a for a in var['anuncios']
                 if a['status'] == 'VERDE' and not a.get('novo') and (a.get('freq') or 0) < 2.8],
                key=lambda a: -(a.get('gasto') or 0))[:3]
if verdes:
    L.append('🟢 PRONTOS P/ VALIDAR ESCALA (verdes maduros, sem fadiga — conferir custo/comparecimento na Central):')
    for a in verdes:
        L.append(f"• {nome(a)} [{a['celula']}] — R$ {a['custo']:.2f}/{a.get('metrica','res')} · "
                 f"gasto 7d {brl(a.get('gasto') or 0)}")
    L.append('')

# ---- 3. Delta vs ontem ---------------------------------------------------
if ontem:
    exc_hoje = {s for s, a in ads.items() if a['status'] in EXC}
    exc_ontem = {s for s, r in h_ontem.items() if r['s'] in EXC}
    novos = exc_hoje - exc_ontem
    resolvidos, banda = [], []
    for sid in exc_ontem - exc_hoje:
        if sid not in ads:
            resolvidos.append(sid)      # sumiu da leitura = sem gasto = pausado/desligado
        elif ads[sid]['status'] in ('VERDE', 'AMARELO', 'AMOSTRA'):
            banda.append(sid)           # continuou rodando e voltou pra banda
    delta = f'Δ vs {ontem}: {len(novos)} novo(s) alerta(s)'
    if resolvidos:
        delta += f' · {len(resolvidos)} resolvido(s) ✓ (saiu do ar)'
    if banda:
        delta += f' · {len(banda)} voltou pra banda'
    L.append(delta)
else:
    L.append('Primeira leitura com histórico — comparativo diário começa amanhã.')

r = var.get('resumo', {})
L.append(f"Panorama: {r.get('VERDE',0)} verdes · {r.get('AMARELO',0)} amarelos · "
         f"{(r.get('AMOSTRA',0)+(r.get('AGUARDANDO',0)))} sem amostra · "
         f"{len(var.get('anuncios',[]))} anúncios ativos")
L.append(f'Central: {CENTRAL}')

texto = '\n'.join(L)

# ---- 4. Posta no ClickUp -------------------------------------------------
if '--dry' in sys.argv:
    print(texto)
    sys.exit(0)

mcp = json.loads(Path(r'C:\Users\vinic\.mcp.json').read_text(encoding='utf-8'))
token = mcp['mcpServers']['agencia']['env']['CLICKUP_API_TOKEN']
req = urllib.request.Request(
    f'https://api.clickup.com/api/v2/task/{TASK_ID}/comment',
    data=json.dumps({'comment_text': texto, 'notify_all': False}).encode('utf-8'),
    headers={'Authorization': token, 'Content-Type': 'application/json'},
    method='POST')
try:
    with urllib.request.urlopen(req, timeout=45) as resp:
        ok = resp.status in (200, 201)
    print(f'analise postada no ClickUp ({TASK_ID}): {"ok" if ok else resp.status}')
except Exception as e:
    print(f'analise gerada; falha ao postar no ClickUp: {e}', file=sys.stderr)
    print(texto)
    sys.exit(1)
