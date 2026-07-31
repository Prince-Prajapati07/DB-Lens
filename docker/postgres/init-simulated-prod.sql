CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  amount NUMERIC(12, 2) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orders (customer_id, status, amount, created_at)
SELECT
  (random() * 250)::int + 1,
  CASE WHEN i % 5 = 0 THEN 'pending' WHEN i % 7 = 0 THEN 'cancelled' ELSE 'paid' END,
  round((random() * 500 + 10)::numeric, 2),
  now() - (i || ' minutes')::interval
FROM generate_series(1, 1000) AS i;

SELECT COUNT(*) FROM orders;
SELECT status, COUNT(*), SUM(amount) FROM orders GROUP BY status;
SELECT * FROM orders WHERE customer_id = 42 ORDER BY created_at DESC LIMIT 20;
