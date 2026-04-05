CREATE DATABASE IF NOT EXISTS smart_cart_system;
USE smart_cart_system;

-- NOTE: Table names are case-sensitive in Linux containers.
-- app.py uses uppercase table names (CART, PRODUCT, ...), so we create them uppercase here.

CREATE TABLE IF NOT EXISTS CART (
  cart_id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY,
  mac_address VARCHAR(100) NOT NULL UNIQUE,
  cart_label VARCHAR(10) UNIQUE,
  pin VARCHAR(6)
);

CREATE TABLE IF NOT EXISTS PRODUCT (
  product_id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY,
  barcode VARCHAR(50) NOT NULL UNIQUE,
  name VARCHAR(255) NOT NULL,
  unit_price DECIMAL(10,2) NOT NULL,
  expected_weight FLOAT NOT NULL,
  stock_quantity INT(11) NOT NULL,
  weight INT(11) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS SHOPPING_SESSION (
  session_id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cart_id INT(11) NOT NULL,
  status VARCHAR(20) NOT NULL,
  total_cost DECIMAL(10,2) NOT NULL DEFAULT 0,
  FOREIGN KEY (cart_id) REFERENCES CART(cart_id)
);

CREATE TABLE IF NOT EXISTS CART_ITEM_BRIDGE (
  session_id INT(11) NOT NULL,
  product_id INT(11) NOT NULL,
  quantity INT(11) DEFAULT 1,
  PRIMARY KEY (session_id, product_id),
  FOREIGN KEY (session_id) REFERENCES SHOPPING_SESSION(session_id),
  FOREIGN KEY (product_id) REFERENCES PRODUCT(product_id)
);

CREATE TABLE IF NOT EXISTS ADMIN_ACCOUNT (
  admin_id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Initial admin row is auto-seeded by app.py on first login attempt if missing.

INSERT INTO CART (mac_address, cart_label, pin)
VALUES
  ('AA:BB:CC:DD:EE:01', '01', NULL),
  ('AA:BB:CC:DD:EE:02', '02', NULL)
ON DUPLICATE KEY UPDATE
  mac_address = VALUES(mac_address),
  cart_label = VALUES(cart_label),
  pin = VALUES(pin);

INSERT INTO PRODUCT (barcode, name, unit_price, expected_weight, stock_quantity, weight)
VALUES
  ('7000001', 'Marie Biscuits', 35.00, 120.00, 50, 120),
  ('7000002', 'Parle-G Biscuits', 30.00, 100.00, 60, 100),
  ('7000003', 'Tea Powder', 180.00, 250.00, 40, 250),
  ('7000004', 'Sugar 1kg', 115.00, 1000.00, 35, 1000),
  ('7000005', 'Basmati Rice 5kg', 780.00, 5000.00, 20, 5000),
  ('7000006', 'Lentils (Masoor) 1kg', 165.00, 1000.00, 30, 1000),
  ('7000007', 'Cooking Oil 1L', 240.00, 1000.00, 25, 1000),
  ('7000008', 'Iodized Salt 1kg', 28.00, 1000.00, 45, 1000),
  ('7000009', 'Instant Noodles Pack', 25.00, 80.00, 100, 80),
  ('7000010', 'Milk Powder 500g', 420.00, 500.00, 18, 500)
ON DUPLICATE KEY UPDATE
  name = VALUES(name),
  unit_price = VALUES(unit_price),
  expected_weight = VALUES(expected_weight),
  stock_quantity = VALUES(stock_quantity),
  weight = VALUES(weight);
