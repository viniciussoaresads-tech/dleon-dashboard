import psycopg2
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "funil.json"

conn = psycopg2.connect(
    host='uno-datalake-cluster-1.cp74abrqalt6.sa-east-1.redshift.amazonaws.com',
    port=5439, dbname='uno', user='com8053_reader',
    password='4rCENOaB9c92m1I0cX5AHKmdMd3', sslmode='require', connect_timeout=30
)
cur = conn.cursor()

today = date.today()
start = today - timedelta(days=90)

cur.execute("""
WITH avaliacoes AS (
    SELECT start_date::date AS data,
        CASE WHEN service_name ILIKE '%%Lentes%%'    THEN 'Lentes'
             WHEN service_name ILIKE '%%Protocolo%%' THEN 'Protocolo'
             WHEN service_name ILIKE '%%Harmoniza%%' THEN 'HOF'
             WHEN service_name ILIKE '%%Implant%%'   THEN 'Implante'
        END AS produto, customer_id, status_id
    FROM com8053.appointments
    WHERE schema='clinica_dleon' AND service_budget=true
      AND (service_name ILIKE '%%Avali%%Lentes%%' OR service_name ILIKE '%%Avali%%Protocolo%%'
           OR service_name ILIKE '%%Avali%%Harmoniz%%' OR service_name ILIKE '%%Avali%%Implant%%')
      AND start_date::date BETWEEN %s AND %s
),
funil AS (
    SELECT data, produto, COUNT(*) AS aval,
        COUNT(CASE WHEN status_id=5 THEN 1 END) AS comp
    FROM avaliacoes GROUP BY 1, 2
),
deals_produto AS (
    SELECT d.deal_id, d.converted_at::date AS data,
        COALESCE(CASE WHEN a.service_name ILIKE '%%Lentes%%'    THEN 'Lentes'
            WHEN a.service_name ILIKE '%%Protocolo%%' THEN 'Protocolo'
            WHEN a.service_name ILIKE '%%Harmoniza%%' THEN 'HOF'
            WHEN a.service_name ILIKE '%%Implant%%'   THEN 'Implante' END, 'Outros') AS produto,
        d.total_price
    FROM com8053.deals d
    LEFT JOIN com8053.appointments a
        ON a.customer_id=d.customer_id AND a.start_date::date=d.customer_appointment_date::date
        AND a.schema='clinica_dleon' AND a.service_budget=true
        AND (a.service_name ILIKE '%%Avali%%Lentes%%' OR a.service_name ILIKE '%%Avali%%Protocolo%%'
             OR a.service_name ILIKE '%%Avali%%Harmoniz%%' OR a.service_name ILIKE '%%Avali%%Implant%%'
             OR a.service_name ILIKE '%%Implant%%')
    WHERE d.schema='clinica_dleon' AND d.converted_at IS NOT NULL
      AND d.converted_at::date BETWEEN %s AND %s
),
fechamentos AS (
    SELECT data, produto, COUNT(DISTINCT deal_id) AS fech, SUM(total_price) AS fat
    FROM deals_produto GROUP BY 1, 2
),
todos AS (SELECT data, produto FROM funil UNION SELECT data, produto FROM fechamentos)
SELECT t.data, t.produto,
    COALESCE(f.aval,0), COALESCE(f.comp,0),
    CASE WHEN COALESCE(f.aval,0)>0 THEN ROUND(f.comp::numeric/f.aval*100,1) END,
    COALESCE(fc.fech,0),
    CASE WHEN COALESCE(f.comp,0)>0 THEN ROUND(fc.fech::numeric/f.comp*100,1) END,
    ROUND(COALESCE(fc.fat,0)::numeric,2)
FROM todos t
LEFT JOIN funil f ON f.data=t.data AND f.produto=t.produto
LEFT JOIN fechamentos fc ON fc.data=t.data AND fc.produto=t.produto
ORDER BY t.data, t.produto
""", (str(start), str(today), str(start), str(today)))

rows = cur.fetchall()

data = [{"data":str(r[0]),"produto":r[1],"aval":int(r[2]),"comp":int(r[3]),
          "pct_comp":float(r[4]) if r[4] is not None else None,
          "fech":int(r[5]),"pct_vend":float(r[6]) if r[6] is not None else None,
          "fatur":float(r[7])} for r in rows]

DATA_FILE.parent.mkdir(exist_ok=True)
with open(DATA_FILE, 'w', encoding='utf-8') as f:
    json.dump({"updated_at": today.isoformat(), "start": str(start), "end": str(today), "rows": data}, f, ensure_ascii=False)
print(f"funil OK: {len(data)} linhas")

# Equipe comercial — formato mensal (desde Jan 2026)
from collections import defaultdict
EQ_INI = '2026-01-01'
EQ_FIM = str(today)

cur.execute("""
    SELECT o.seller_name,
           DATE_TRUNC('month', o.created_at)::date AS mes,
           COUNT(DISTINCT o.order_id)           AS vendas,
           ROUND(SUM(o.total_price)::numeric,2) AS faturamento,
           COUNT(DISTINCT o.customer_id)        AS clientes
    FROM com8053.orders o
    WHERE o.schema='clinica_dleon'
      AND o.created_at::date BETWEEN %s AND %s
      AND o.status NOT IN ('CANCELED','ABANDONMENT')
      AND o.total_price > 0
      AND o.seller_name NOT ILIKE '%%Cl%%nica%%'
      AND o.seller_name NOT ILIKE '%%Renegocia%%'
      AND o.seller_name NOT ILIKE '%%Administrador%%'
    GROUP BY o.seller_name, DATE_TRUNC('month', o.created_at)
    ORDER BY o.seller_name, mes
""", (EQ_INI, EQ_FIM))
vd, vt = defaultdict(list), defaultdict(float)
for r in cur.fetchall():
    n = str(r[0]).strip(); vt[n] += float(r[3])
    vd[n].append({'mes': str(r[1])[:7], 'vendas': int(r[2]), 'faturamento': float(r[3]), 'clientes': int(r[4])})
vendedores = [{'nome': n, 'meses': ms} for n, ms in sorted(vd.items(), key=lambda x: -vt[x[0]]) if vt[n] >= 50000]

cur.execute("""
    SELECT d.employee_name,
           DATE_TRUNC('month', d.created_at)::date AS mes,
           COUNT(*)                                                              AS leads,
           COUNT(CASE WHEN d.customer_appointment_date IS NOT NULL THEN 1 END)  AS agendou,
           COUNT(CASE WHEN d.customer_appointment_status_id=5 THEN 1 END)       AS compareceu,
           COUNT(CASE WHEN d.converted_at IS NOT NULL THEN 1 END)               AS convertidos
    FROM com8053.deals d
    WHERE d.schema='clinica_dleon'
      AND d.created_at::date BETWEEN %s AND %s
      AND d.employee_name IS NOT NULL
      AND d.employee_name NOT ILIKE '%%Administrador%%'
    GROUP BY d.employee_name, DATE_TRUNC('month', d.created_at)
    ORDER BY d.employee_name, mes
""", (EQ_INI, EQ_FIM))
sd, st = defaultdict(list), defaultdict(int)
for r in cur.fetchall():
    n = str(r[0]).strip(); st[n] += int(r[2])
    sd[n].append({'mes': str(r[1])[:7], 'leads': int(r[2]), 'agendou': int(r[3]), 'compareceu': int(r[4]), 'convertidos': int(r[5])})
sdrs = [{'nome': n, 'meses': ms} for n, ms in sorted(sd.items(), key=lambda x: -st[x[0]]) if st[n] >= 5]

cur.execute("""
    SELECT a.employee_name,
           DATE_TRUNC('month', a.start_date)::date AS mes,
           COUNT(*)                                                              AS avaliacoes,
           COUNT(CASE WHEN a.status_id=5 THEN 1 END)                            AS compareceram,
           COUNT(CASE WHEN a.status_id=5 AND o.order_id IS NOT NULL
                       AND o.status NOT IN ('CANCELED','ABANDONMENT')
                       AND o.total_price>0 THEN 1 END)                          AS converteram,
           ROUND(SUM(CASE WHEN a.status_id=5 AND o.order_id IS NOT NULL
                       AND o.status NOT IN ('CANCELED','ABANDONMENT')
                       THEN o.total_price ELSE 0 END)::numeric,2)               AS faturamento
    FROM com8053.appointments a
    LEFT JOIN com8053.orders o ON o.order_id=a.order_id AND o.schema='clinica_dleon'
    WHERE a.schema='clinica_dleon' AND a.service_budget=true
      AND a.start_date::date BETWEEN %s AND %s
      AND a.employee_name IS NOT NULL
    GROUP BY a.employee_name, DATE_TRUNC('month', a.start_date)
    ORDER BY a.employee_name, mes
""", (EQ_INI, EQ_FIM))
ad, at_ = defaultdict(list), defaultdict(int)
for r in cur.fetchall():
    n = str(r[0]).strip(); at_[n] += int(r[3])
    ad[n].append({'mes': str(r[1])[:7], 'avaliacoes': int(r[2]), 'compareceram': int(r[3]), 'converteram': int(r[4]), 'faturamento': float(r[5])})
avaliadores = [{'nome': n, 'meses': ms} for n, ms in sorted(ad.items(), key=lambda x: -sum(m['converteram'] for m in x[1])) if at_[n] >= 5]

conn.close()

equipe_file = ROOT / "data" / "equipe.json"
with open(equipe_file, 'w', encoding='utf-8') as f:
    json.dump({'updated_at': EQ_FIM, 'start': EQ_INI, 'end': EQ_FIM,
               'vendedores': vendedores, 'sdrs': sdrs, 'avaliadores': avaliadores}, f, ensure_ascii=False)
print(f"equipe OK: {len(vendedores)} vendedores | {len(sdrs)} SDRs | {len(avaliadores)} avaliadores")

# Git push
result = subprocess.run(
    ['git', '-C', str(ROOT), 'add', 'data/funil.json', 'data/equipe.json'],
    capture_output=True, text=True
)
result = subprocess.run(
    ['git', '-C', str(ROOT), 'commit', '-m', f'chore: dados {today}'],
    capture_output=True, text=True
)
if 'nothing to commit' in result.stdout:
    print("Sem mudancas nos dados.")
    sys.exit(0)

result = subprocess.run(
    ['git', '-C', str(ROOT), 'push'],
    capture_output=True, text=True
)
if result.returncode != 0:
    print("Erro no push:", result.stderr)
    sys.exit(1)

print("Push OK - GitHub Pages atualizado.")
