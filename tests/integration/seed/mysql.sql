CREATE TABLE customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(200) NOT NULL UNIQUE,
    name TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
CREATE TABLE orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    note TEXT,
    KEY ix_orders_customer (customer_id),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE audit_log (msg TEXT, at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB;
CREATE VIEW v_customer_totals AS
    SELECT c.id, c.email, COALESCE(SUM(o.amount), 0) AS total
    FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.email;
CREATE TRIGGER trg_orders_log AFTER INSERT ON orders FOR EACH ROW INSERT INTO audit_log(msg) VALUES (CONCAT('order ', NEW.id));
INSERT INTO customers(email, name)
    WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 200)
    SELECT CONCAT('user', n, '@example.com'), CONCAT('User ', n) FROM seq;
INSERT INTO orders(customer_id, amount, note)
    WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 1000)
    SELECT (n % 200) + 1, n * 1.5, CONCAT('n', n) FROM seq;
