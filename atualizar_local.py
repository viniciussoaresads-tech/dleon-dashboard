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
conn.close()

data = [{"data":str(r[0]),"produto":r[1],"aval":int(r[2]),"comp":int(r[3]),
          "pct_comp":float(r[4]) if r[4] is not None else None,
          "fech":int(r[5]),"pct_vend":float(r[6]) if r[6] is not None else None,
          "fatur":float(r[7])} for r in rows]

DATA_FILE.parent.mkdir(exist_ok=True)
with open(DATA_FILE, 'w', encoding='utf-8') as f:
    json.dump({"updated_at": today.isoformat(), "start": str(start), "end": str(today), "rows": data}, f, ensure_ascii=False)

print(f"OK: {len(data)} linhas ({start} a {today})")

# Git push
result = subprocess.run(
    ['git', '-C', str(ROOT), 'add', 'data/funil.json'],
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
