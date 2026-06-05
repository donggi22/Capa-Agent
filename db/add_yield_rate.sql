USE mes_db;

ALTER TABLE molds
    ADD COLUMN yield_rate DECIMAL(4,3) NOT NULL DEFAULT 1.000;

UPDATE molds SET yield_rate = 0.950 WHERE product_code = 'PROD-A01';
UPDATE molds SET yield_rate = 0.970 WHERE product_code = 'PROD-B01';
UPDATE molds SET yield_rate = 0.910 WHERE product_code = 'PROD-C01';
