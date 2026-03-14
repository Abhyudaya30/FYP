from flask import Flask, render_template, request, jsonify, redirect, url_for
import mysql.connector

app = Flask(__name__)

# --- DATABASE CONFIGURATION ---
db_config = {
    'host': '127.0.0.1',
    'user': 'root',
    'password': '',
    'database': 'smart_cart_system',
    'port': 3306
}

# In-memory storage for security flags
pending_placement = {} 
pending_removal = {} # Added for removal tracking
security_alerts = {} # Added for popup tracking

def get_db_connection():
    return mysql.connector.connect(**db_config)

# --- PAGE ROUTES ---

@app.route('/')
def landing_page():
    return render_template('landing.html')

@app.route('/start')
def auto_assign_cart():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT c.cart_label 
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s ON c.cart_id = s.cart_id AND s.status = 'active'
            WHERE s.session_id IS NULL
            ORDER BY c.cart_label ASC
            LIMIT 1
        """
        cursor.execute(query)
        available_cart = cursor.fetchone()
        conn.close()

        if available_cart:
            return redirect(url_for('show_cart', label=available_cart['cart_label']))
        else:
            return "<h1>All carts are currently in use. Please wait for a moment!</h1>", 200
            
    except Exception as e:
        return f"System Error: {str(e)}"

@app.route('/cart/<label>')
def show_cart(label):
    return render_template('cart.html', cart_label=label)

@app.route('/cashier')
def cashier_page():
    return render_template('cashier.html')

@app.route('/success')
def success_page():
    return render_template('success.html')

@app.route('/admin/inventory')
def admin_inventory():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM PRODUCT")
        products = cursor.fetchall()
        conn.close()
        return render_template('inventory.html', products=products)
    except Exception as e:
        return f"Database Error: {str(e)}"

# --- API ROUTES ---

@app.route('/api/all_carts_status')
def all_carts_status():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT c.cart_label, IFNULL(s.status, 'idle') as status, IFNULL(s.total_cost, 0) as total_cost
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s ON c.cart_id = s.cart_id AND s.status = 'active'
            ORDER BY c.cart_label ASC
        """
        cursor.execute(query)
        carts = cursor.fetchall()
        conn.close()
        return jsonify(carts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/reset_everything', methods=['POST'])
def reset_everything():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE SHOPPING_SESSION SET status = 'completed' WHERE status = 'active'")
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get_cart/<label>')
def get_cart_data(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT p.name, p.unit_price, b.quantity, p.barcode 
            FROM CART_ITEM_BRIDGE b 
            JOIN PRODUCT p ON b.product_id = p.product_id 
            JOIN SHOPPING_SESSION s ON b.session_id = s.session_id
            JOIN CART c ON s.cart_id = c.cart_id
            WHERE c.cart_label = %s AND s.status = 'active'
        """
        cursor.execute(query, (label,))
        items = cursor.fetchall()
        total = sum(item['unit_price'] * item['quantity'] for item in items)
        conn.close()
        return jsonify({"items": items, "total": float(total)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/end_session/<label>', methods=['POST'])
def end_session(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE SHOPPING_SESSION s
            JOIN CART c ON s.cart_id = c.cart_id
            SET s.status = 'completed'
            WHERE c.cart_label = %s AND s.status = 'active'
        """, (label,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/update_product', methods=['POST'])
def update_product():
    data = request.json
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = "UPDATE PRODUCT SET unit_price = %s, stock_quantity = %s, weight = %s WHERE product_id = %s"
        cursor.execute(query, (data['price'], data['stock'], data['weight'], data['id']))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/add_product', methods=['POST'])
def add_product():
    data = request.json
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            INSERT INTO PRODUCT (name, barcode, unit_price, stock_quantity, weight) 
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(query, (data['name'], data['barcode'], data['price'], data['stock'], data['weight']))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/delete_product/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM PRODUCT WHERE product_id = %s", (product_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get_product_info/<barcode>')
def get_product_info(barcode):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, unit_price, weight FROM PRODUCT WHERE barcode = %s", (barcode,))
        product = cursor.fetchone()
        conn.close()
        if product:
            return jsonify({
                "status": "success", 
                "name": product['name'], 
                "price": product['unit_price'],
                "weight": product['weight']
            })
        return jsonify({"status": "error", "message": "Product not found"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/scan', methods=['POST'])
def scan_item():
    data = request.json
    barcode = data.get('barcode')
    label = data.get('cart_label')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT product_id, unit_price, stock_quantity FROM PRODUCT WHERE barcode = %s", (barcode,))
    product = cursor.fetchone()

    if not product:
        conn.close()
        return jsonify({"status": "error", "message": "Product barcode not found"}), 404
    
    if product['stock_quantity'] <= 0:
        conn.close()
        return jsonify({"status": "error", "message": "Out of stock"}), 400

    cursor.execute("""
        SELECT s.session_id FROM SHOPPING_SESSION s 
        JOIN CART c ON s.cart_id = c.cart_id 
        WHERE c.cart_label = %s AND s.status = 'active'
    """, (label,))
    session = cursor.fetchone()
    
    if not session:
        cursor.execute("SELECT cart_id FROM CART WHERE cart_label = %s", (label,))
        cart_row = cursor.fetchone()
        if not cart_row:
            conn.close()
            return jsonify({"status": "error", "message": "Cart label does not exist"}), 404
            
        cursor.execute("INSERT INTO SHOPPING_SESSION (cart_id, status, total_cost) VALUES (%s, 'active', 0)", (cart_row['cart_id'],))
        conn.commit()
        session_id = cursor.lastrowid
    else:
        session_id = session['session_id']

    cursor.execute("""
        INSERT INTO CART_ITEM_BRIDGE (session_id, product_id, quantity) 
        VALUES (%s, %s, 1) 
        ON DUPLICATE KEY UPDATE quantity = quantity + 1
    """, (session_id, product['product_id']))
    
    cursor.execute("UPDATE SHOPPING_SESSION SET total_cost = total_cost + %s WHERE session_id = %s", 
                   (product['unit_price'], session_id))
    
    cursor.execute("UPDATE PRODUCT SET stock_quantity = stock_quantity - 1 WHERE product_id = %s", (product['product_id'],))
    
    pending_placement[label] = True

    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"Barcode {barcode} added"})

# --- NEW SECURITY ROUTES ---

@app.route('/api/remove_item', methods=['POST'])
def remove_item():
    data = request.json
    barcode = data.get('barcode')
    label = data.get('cart_label')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT product_id, unit_price FROM PRODUCT WHERE barcode = %s", (barcode,))
    product = cursor.fetchone()
    
    cursor.execute("""
        SELECT s.session_id FROM SHOPPING_SESSION s JOIN CART c ON s.cart_id = c.cart_id 
        WHERE c.cart_label = %s AND s.status = 'active'
    """, (label,))
    session = cursor.fetchone()

    if product and session:
        cursor.execute("UPDATE CART_ITEM_BRIDGE SET quantity = quantity - 1 WHERE session_id = %s AND product_id = %s", (session['session_id'], product['product_id']))
        cursor.execute("DELETE FROM CART_ITEM_BRIDGE WHERE quantity <= 0")
        cursor.execute("UPDATE SHOPPING_SESSION SET total_cost = total_cost - %s WHERE session_id = %s", (product['unit_price'], session['session_id']))
        pending_removal[label] = True # Alert ESP32 to expect a removal
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    conn.close()
    return jsonify({"status": "error"})

@app.route('/api/report_alert/<label>', methods=['POST'])
def report_alert(label):
    security_alerts[label] = True
    return jsonify({"status": "alert_received"})

@app.route('/api/check_alert/<label>')
def check_alert(label):
    return jsonify({"alert": security_alerts.get(label, False)})

@app.route('/api/clear_alert/<label>', methods=['POST'])
def clear_alert(label):
    security_alerts[label] = False
    return jsonify({"status": "cleared"})

@app.route('/api/confirm_removal/<label>', methods=['POST'])
def confirm_removal(label):
    pending_removal[label] = False
    return jsonify({"status": "verified"})

@app.route('/api/confirm_placement/<label>', methods=['POST'])
def confirm_placement(label):
    pending_placement[label] = False
    return jsonify({"status": "verified"})

# --- OPTIMIZED SUPER ROUTE ---
@app.route('/api/cart_update/<label>')
def cart_update(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = "SELECT s.status FROM SHOPPING_SESSION s JOIN CART c ON s.cart_id = c.cart_id WHERE c.cart_label = %s AND s.status = 'active' LIMIT 1"
        cursor.execute(query, (label,))
        row = cursor.fetchone()
        conn.close()

        if row:
            session_status = row['status']
            is_pending = pending_placement.get(label, False)
            is_removing = pending_removal.get(label, False)
        else:
            session_status = "idle"
            is_pending = False
            is_removing = False
            
        return f"{session_status}|{str(is_pending).lower()}|{str(is_removing).lower()}"
    except Exception as e:
        return "error|false|false"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)