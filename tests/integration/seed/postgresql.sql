CREATE TABLE customers (
    id serial PRIMARY KEY,
    email varchar(200) NOT NULL UNIQUE,
    name text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders (
    id bigserial PRIMARY KEY,
    customer_id int NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    amount numeric(12,2) NOT NULL,
    note text
);
CREATE INDEX ix_orders_customer ON orders(customer_id);
CREATE TABLE audit_log (msg text, at timestamptz DEFAULT now());
CREATE VIEW v_customer_totals AS
    SELECT c.id, c.email, coalesce(sum(o.amount), 0) AS total
    FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.email;
CREATE FUNCTION log_order() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN INSERT INTO audit_log(msg) VALUES ('order ' || NEW.id); RETURN NEW; END $$;
CREATE TRIGGER trg_orders_log AFTER INSERT ON orders FOR EACH ROW EXECUTE FUNCTION log_order();
INSERT INTO customers(email, name) SELECT 'user' || g || '@example.com', 'User ' || g FROM generate_series(1, 200) g;
INSERT INTO orders(customer_id, amount, note) SELECT (g % 200) + 1, g * 1.5, 'n' || g FROM generate_series(1, 1000) g;
