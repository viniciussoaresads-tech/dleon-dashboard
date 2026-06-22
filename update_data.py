import psycopg2
import json
import os
from datetime import date, timedelta

conn = psycopg2.connect(
    host=os.environ['REDSHIFT_HOST'],
    port=int(os.environ.get('REDSHIFT_PORT', 5439)),
    dbname=os.environ.get('REDSHIFT_DB', 'uno'),
    user=os.environ['REDSHIFT_USER'],
    password=os.environ['REDSHIFT_PASSWORD'],
    sslmode='require',
    connect_timeout=30
)
cur = conn.cursor()

today = date.today()
start = today - timedelta(days=90)

cur.execute("""
WITH avaliacoes AS (
    SELECT start_date::date AS data,
        CASE
            WHEN service_name ILIKE '%Lentes%'    THEN 'Lentes'
            WHEN service_name ILIKE '%Protocolo%' THEN 'Protocolo'
            WHEN service_name ILIKE '%Harmoniza%' THEN 'HOF'
            WHEN service_name ILIKE '%Implant%'   THEN 'Implante'
        END AS produto,
        customer_id, status_id
    FROM com8053.appointments
    WHERE schema = 'clinica_dleon' AND service_budget = true
      AND (
          service_name ILIKE '%Avali%Lentes%'
          OR service_name ILIKE '%Avali%Protocolo%'
          OR service_name ILIKE '%Avali%Harmoniz%'
          OR service_name ILIKE '%Avali%Implant%'
      )
      AND start_date::date BETWEEN %s AND %s
),
funil AS (
    SELECT data, produto,
        COUNT(*) AS avaliacoes,
        COUNT(CASE WHEN status_id = 5 THEN 1 END) AS comparecimentos,
        COUNT(CASE WHEN status_id IN (6,8) THEN 1 END) AS faltas
    FROM avaliacoes GROUP BY 1, 2
),
deals_produto AS (
    SELECT d.deal_id, d.converted_at::date AS data,
        COALESCE(CASE
            WHEN a.service_name ILIKE '%Lentes%'    THEN 'Lentes'
            WHEN a.service_name ILIKE '%Protocolo%' THEN 'Protocolo'
            WHEN a.service_name ILIKE '%Harmoniza%' THEN 'HOF'
            WHEN a.service_name ILIKE '%Implant%'   THEN 'Implante'
        END, 'Outros') AS produto,
        d.total_price
    FROM com8053.deals d
    LEFT JOIN com8053.appointments a
        ON  a.customer_id      = d.customer_id
        AND a.start_date::date = d.customer_appointment_date::date
        AND a.schema           = 'clinica_dleon'
        AND a.service_budget   = true
        AND (
            a.service_name ILIKE '%Avali%Lentes%'
            OR a.service_name ILIKE '%Avali%Protocolo%'
            OR a.service_name ILIKE '%Avali%Harmoniz%'
            OR a.service_name ILIKE '%Avali%Implant%'
            OR a.service_name ILIKE '%Implant%'
        )
    WHERE d.schema        = 'clinica_dleon'
      AND d.converted_at IS NOT NULL
      AND d.converted_at::date BETWEEN %s AND %s
),
fechamentos AS (
    SELECT data, produto, COUNT(DISTINCT deal_id) AS fechamentos, SUM(total_price) AS faturamento
    FROM deals_produto GROUP BY 1, 2
),
todos AS (SELECT data, produto FROM funil UNION SELECT data, produto FROM fechamentos)
SELECT t.data, t.produto,
    COALESCE(f.avaliacoes, 0),
    COALESCE(f.comparecimentos, 0),
    CASE WHEN COALESCE(f.avaliacoes,0) > 0
        THEN ROUND(f.comparecimentos::numeric / f.avaliacoes * 100, 1) END,
    COALESCE(fc.fechamentos, 0),
    CASE WHEN COALESCE(f.comparecimentos,0) > 0
        THEN ROUND(fc.fechamentos::numeric / f.comparecimentos * 100, 1) END,
    ROUND(COALESCE(fc.faturamento, 0)::numeric, 2)
FROM todos t
LEFT JOIN funil f       ON f.data  = t.data AND f.produto  = t.produto
LEFT JOIN fechamentos fc ON fc.data = t.data AND fc.produto = t.produto
ORDER BY t.data, t.produto
""", (str(start), str(today), str(start), str(today)))

rows = cur.fetchall()
data = []
for r in rows:
    data.append({
        "data": str(r[0]),
        "produto": r[1],
        "aval": int(r[2]),
        "comp": int(r[3]),
        "pct_comp": float(r[4]) if r[4] is not None else None,
        "fech": int(r[5]),
        "pct_vend": float(r[6]) if r[6] is not None else None,
        "fatur": float(r[7])
    })

os.makedirs('data', exist_ok=True)
with open('data/funil.json', 'w', encoding='utf-8') as f:
    json.dump({"updated_at": today.isoformat(), "start": str(start), "end": str(today), "rows": data}, f, ensure_ascii=False)

conn.close()
print(f"OK: {len(data)} linhas exportadas ({start} a {today})")
