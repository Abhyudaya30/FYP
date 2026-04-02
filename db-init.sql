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
