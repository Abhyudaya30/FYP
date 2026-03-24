from flask import Flask, render_template, request, jsonify, redirect, url_for
import mysql.connector
import random
from functools import wraps

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
pending_removal = {}
security_alerts = {}
# Added to track the weight of the item currently being scanned/removed
expected_weight_change = {} 

def get_db_connection():
    return mysql.connector.connect(**db_config)

# --- PIN HELPER ---
def generate_pin():
    return str(random.randint(1000, 9999))

# --- NETWORK SECURITY ---
ALLOWED_NETWORKS = [
    "192.168.1.",
    "127.0.0.1",
]

def is_allowed_network():
    client_ip = request.remote_addr
    for network in ALLOWED_NETWORKS:
        if client_ip.startswith(network):
            return True
    return False

def store_network_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_allowed_network():
            return render_template(
                '403.html',
                client_ip=request.remote_addr
            ), 403
        return f(*args, **kwargs)
    return decorated

# --- PAGE ROUTES ---

@app.route('/')
def landing_page():
    return render_template('landing.html')

@app.route('/start')
@store_network_required
def auto_assign_cart():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT c.cart_label, c.cart_id
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s 
            ON c.cart_id = s.cart_id 
            AND s.status = 'active'
            WHERE s.session_id IS NULL
            ORDER BY c.cart_label ASC
            LIMIT 1
        """
        cursor.execute(query)
        available_cart = cursor.fetchone()

        if available_cart:
            pin = generate_pin()
            print("\n" + "="*40)
            print(f"DEBUG: Generated PIN for Cart {available_cart['cart_label']} is: {pin}")
            print("="*40 + "\n")
            
            cursor.execute("""
                UPDATE CART SET pin = %s 
                WHERE cart_id = %s
            """, (pin, available_cart['cart_id']))
            conn.commit()
            conn.close()

            return redirect(url_for(
                'pin_page',
                label=available_cart['cart_label']
            ))
        else:
            conn.close()
            return "<h1>All carts are currently in use!</h1>", 200

    except Exception as e:
        return f"System Error: {str(e)}"

@app.route('/pin/<label>')
@store_network_required 
def pin_page(label):
    return render_template('pin.html', cart_label=label)

@app.route('/verify_pin', methods=['POST'])
@store_network_required
def verify_pin():
    data = request.json
    label = data.get('cart_label')
    entered_pin = data.get('pin')

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT cart_id, pin FROM CART WHERE cart_label = %s", (label,))
        cart = cursor.fetchone()

        if cart and cart['pin'] == entered_pin:
            cursor.execute("""
                SELECT session_id FROM SHOPPING_SESSION 
                WHERE cart_id = %s AND status = 'active'
            """, (cart['cart_id'],))
            existing_session = cursor.fetchone()

            if not existing_session:
                cursor.execute("""
                    INSERT INTO SHOPPING_SESSION (cart_id, status, total_cost) 
                    VALUES (%s, 'active', 0)
                """, (cart['cart_id'],))
                conn.commit()

            conn.close()
            return jsonify({'status': 'success'})
        else:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Wrong PIN!'}), 401
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/cart/<label>')
@store_network_required
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
            SELECT c.cart_label, 
            IFNULL(s.status, 'idle') as status, 
            IFNULL(s.total_cost, 0) as total_cost
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s 
            ON c.cart_id = s.cart_id 
            AND s.status = 'active'
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
        cursor.execute("""
            UPDATE SHOPPING_SESSION 
            SET status = 'completed' 
            WHERE status = 'active'
        """)
        cursor.execute("UPDATE CART SET pin = NULL")
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
            SELECT p.name, p.unit_price, 
            b.quantity, p.barcode 
            FROM CART_ITEM_BRIDGE b 
            JOIN PRODUCT p 
            ON b.product_id = p.product_id 
            JOIN SHOPPING_SESSION s 
            ON b.session_id = s.session_id
            JOIN CART c 
            ON s.cart_id = c.cart_id
            WHERE c.cart_label = %s 
            AND s.status = 'active'
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
            WHERE c.cart_label = %s 
            AND s.status = 'active'
        """, (label,))
        cursor.execute("""
            UPDATE CART SET pin = NULL 
            WHERE cart_label = %s
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
        query = """
            UPDATE PRODUCT 
            SET name = %s, barcode = %s, unit_price = %s, 
            stock_quantity = %s, weight = %s 
            WHERE product_id = %s
        """
        cursor.execute(query, (
            data['name'],
            data['barcode'],
            data['price'],
            data['stock'],
            data['weight'],
            data['id']
        ))
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

# --- UPDATE IN app.py ---

@app.route('/api/get_pin/<label>')
def get_pin(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Ensure the label matches exactly what the ESP32 sends (e.g., "A1")
        cursor.execute("SELECT pin FROM CART WHERE cart_label = %s", (label,))
        cart = cursor.fetchone()
        conn.close()
        
        if cart and cart['pin']:
            return jsonify({'pin': cart['pin']})
        else:
            # Explicitly tell the ESP32 that the PIN isn't ready
            return jsonify({'pin': "WAIT"}) 
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/scan', methods=['POST'])
def scan_item():
    data = request.json
    barcode = data.get('barcode')
    label = data.get('cart_label')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Added 'weight' to the query so we can send it to the threshold logic
    cursor.execute("SELECT product_id, unit_price, stock_quantity, weight FROM PRODUCT WHERE barcode = %s", (barcode,))
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
        cursor.execute("INSERT INTO SHOPPING_SESSION (cart_id, status, total_cost) VALUES (%s, 'active', 0)", (cart_row['cart_id'],))
        conn.commit()
        session_id = cursor.lastrowid
    else:
        session_id = session['session_id']

    cursor.execute("""
        INSERT INTO CART_ITEM_BRIDGE (session_id, product_id, quantity)
        VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE quantity = quantity + 1
    """, (session_id, product['product_id']))

    cursor.execute("UPDATE SHOPPING_SESSION SET total_cost = total_cost + %s WHERE session_id = %s", (product['unit_price'], session_id))
    cursor.execute("UPDATE PRODUCT SET stock_quantity = stock_quantity - 1 WHERE product_id = %s", (product['product_id'],))

    pending_placement[label] = True
    # Store the item's weight to calculate threshold later
    expected_weight_change[label] = product['weight'] 

    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"Barcode {barcode} added"})

@app.route('/api/remove_item', methods=['POST'])
def remove_item():
    data = request.json
    barcode = data.get('barcode')
    label = data.get('cart_label')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT product_id, unit_price, weight FROM PRODUCT WHERE barcode = %s", (barcode,))
    product = cursor.fetchone()

    cursor.execute("""
        SELECT s.session_id FROM SHOPPING_SESSION s 
        JOIN CART c ON s.cart_id = c.cart_id
        WHERE c.cart_label = %s AND s.status = 'active'
    """, (label,))
    session = cursor.fetchone()

    if product and session:
        cursor.execute("UPDATE CART_ITEM_BRIDGE SET quantity = quantity - 1 WHERE session_id = %s AND product_id = %s", (session['session_id'], product['product_id']))
        cursor.execute("DELETE FROM CART_ITEM_BRIDGE WHERE quantity <= 0")
        cursor.execute("UPDATE SHOPPING_SESSION SET total_cost = total_cost - %s WHERE session_id = %s", (product['unit_price'], session['session_id']))
        
        pending_removal[label] = True
        # Store the item's weight to calculate removal threshold
        expected_weight_change[label] = product['weight']

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
    expected_weight_change[label] = 0.0 # Clear tracking
    return jsonify({"status": "verified"})

@app.route('/api/confirm_placement/<label>', methods=['POST'])
def confirm_placement(label):
    pending_placement[label] = False
    expected_weight_change[label] = 0.0 # Clear tracking
    return jsonify({"status": "verified"})

@app.route('/api/cart_update/<label>')
def cart_update(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT s.status FROM SHOPPING_SESSION s 
            JOIN CART c ON s.cart_id = c.cart_id
            WHERE c.cart_label = %s AND s.status = 'active' LIMIT 1
        """, (label,))
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

        # --- DYNAMIC 5% THRESHOLD LOGIC ---
        # If an item is being added/removed, threshold = 30g base + 5% of item weight
        # If cart is idle, use a base drift threshold of 35g
        item_w = expected_weight_change.get(label, 0.0)
        
        if is_pending or is_removing:
            threshold = 30.0 + (abs(item_w) * 0.05)
        else:
            threshold = 35.0

        # Return status|pending|removing|threshold
        return f"{session_status}|{str(is_pending).lower()}|{str(is_removing).lower()}|{threshold:.1f}"
    except Exception as e:
        return "error|false|false|35.0"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)