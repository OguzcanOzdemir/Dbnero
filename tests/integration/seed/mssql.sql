CREATE TABLE dbo.customers (
    id INT IDENTITY(1,1) PRIMARY KEY,
    email NVARCHAR(200) NOT NULL UNIQUE,
    name NVARCHAR(MAX),
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
CREATE TABLE dbo.orders (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES dbo.customers(id) ON DELETE CASCADE,
    amount DECIMAL(12,2) NOT NULL,
    note NVARCHAR(MAX)
);
GO
CREATE INDEX ix_orders_customer ON dbo.orders(customer_id);
GO
CREATE TABLE dbo.audit_log (msg NVARCHAR(400), at DATETIME2 DEFAULT SYSUTCDATETIME());
GO
CREATE VIEW dbo.v_customer_totals AS
    SELECT c.id, c.email, COALESCE(SUM(o.amount), 0) AS total
    FROM dbo.customers c LEFT JOIN dbo.orders o ON o.customer_id = c.id GROUP BY c.id, c.email;
GO
CREATE TRIGGER dbo.trg_orders_log ON dbo.orders AFTER INSERT AS
    INSERT INTO dbo.audit_log(msg) SELECT CONCAT('order ', id) FROM inserted;
GO
WITH seq AS (SELECT TOP 200 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) n FROM sys.all_objects)
INSERT INTO dbo.customers(email, name) SELECT CONCAT('user', n, '@example.com'), CONCAT('User ', n) FROM seq;
GO
WITH seq AS (SELECT TOP 1000 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) n FROM sys.all_objects a CROSS JOIN sys.all_objects b)
INSERT INTO dbo.orders(customer_id, amount, note) SELECT (n % 200) + 1, n * 1.5, CONCAT('n', n) FROM seq;
GO
