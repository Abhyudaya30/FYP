from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import mysql.connector
import random

app = Flask(__name__)
app.secret_key = "smartcart-secret-key"

db_config = {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "smartcart",
    "password": "smartcartpass",
    "database": "smart_cart_system",
}

AUTH_USERNAME = "Admin"
AUTH_PASSWORD = "Password123"

# In-memory bridge between web + hardware validation
pending_placement = {}
pending_removal = {}
security_alerts = {}
expected_weight_change = {}
checkout_requests = {}


def get_db_connection():
    return mysql.connector.connect(**db_config)


def generate_pin():
    return str(random.randint(1000, 9999))


def active_session_for_label(cursor, label):
    cursor.execute(
        """
        SELECT s.session_id, s.status, s.cart_id
        FROM SHOPPING_SESSION s
        JOIN CART c ON c.cart_id = s.cart_id
        WHERE c.cart_label = %s AND s.status = 'active'
        LIMIT 1
        """,
        (label,),
    )
    return cursor.fetchone()


def product_by_barcode(cursor, barcode):
    cursor.execute(
        """
        SELECT product_id, name, unit_price, stock_quantity, weight
        FROM PRODUCT
        WHERE barcode = %s
        """,
        (barcode,),
    )
    return cursor.fetchone()


def set_hw_state(label, placement=None, removal=None, expected_weight=None, alert=None):
    if placement is not None:
        pending_placement[label] = bool(placement)
    if removal is not None:
        pending_removal[label] = bool(removal)
    if expected_weight is not None:
        expected_weight_change[label] = float(expected_weight)
    if alert is not None:
        security_alerts[label] = bool(alert)


def hw_state(label):
    return {
        "pending_placement": pending_placement.get(label, False),
        "pending_removal": pending_removal.get(label, False),
        "expected_weight_change": float(expected_weight_change.get(label, 0.0)),
        "alert": security_alerts.get(label, False),
    }


def checkout_requested(label):
    return checkout_requests.get(label, False)


# ------------------------- Pages -------------------------
@app.route("/")
def landing_page():
    return render_template("landing.html")


@app.route("/cashier")
def cashier_landing_page():
    if session.get("cashier_authenticated"):
        return redirect(url_for("cashier_page"))
    return render_template("cashier_landing.html", login_error=None)


@app.route("/cashier", methods=["POST"])
def cashier_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        session["cashier_authenticated"] = True
        return redirect(url_for("cashier_page"))
    return render_template(
        "cashier_landing.html",
        login_error="Invalid username or password.",
    ), 401


@app.route("/admin/inventory")
def inventory_landing_page():
    if session.get("inventory_authenticated"):
        return redirect(url_for("admin_inventory"))
    return render_template("inventory_landing.html", login_error=None)


@app.route("/admin/inventory", methods=["POST"])
def inventory_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        session["inventory_authenticated"] = True
        return redirect(url_for("admin_inventory"))
    return render_template(
        "inventory_landing.html",
        login_error="Invalid username or password.",
    ), 401


@app.route("/start")
def auto_assign_cart():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT c.cart_id, c.cart_label
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s
              ON s.cart_id = c.cart_id
             AND s.status = 'active'
            WHERE s.session_id IS NULL
            ORDER BY c.cart_label ASC
            LIMIT 1
            """
        )
        available = cursor.fetchone()
        if not available:
            conn.close()
            return "<h1>All carts in use!</h1>", 200

        pin = generate_pin()
        cursor.execute(
            "UPDATE CART SET pin = %s WHERE cart_id = %s",
            (pin, available["cart_id"]),
        )
        conn.commit()
        conn.close()

        print(f"\n{'=' * 40}")
        print(f"PIN for Cart {available['cart_label']}: {pin}")
        print(f"{'=' * 40}\n")
        return redirect(url_for("pin_page", label=available["cart_label"]))
    except Exception as e:
        return f"Error: {str(e)}"


@app.route("/pin/<label>")
def pin_page(label):
    return render_template("pin.html", cart_label=label)


@app.route("/cart/<label>")
def show_cart(label):
    return render_template("cart.html", cart_label=label)


@app.route("/bill/<label>")
def show_bill(label):
    return render_template("bill.html", cart_label=label)


@app.route("/cashier/dashboard")
def cashier_page():
    if not session.get("cashier_authenticated"):
        return redirect(url_for("cashier_landing_page"))
    return render_template("cashier.html")


@app.route("/success")
def success_page():
    return render_template("success.html", cart_label=request.args.get("label", ""))


@app.route("/admin/inventory/dashboard")
def admin_inventory():
    if not session.get("inventory_authenticated"):
        return redirect(url_for("inventory_landing_page"))
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM PRODUCT ORDER BY product_id ASC")
        products = cursor.fetchall()
        conn.close()
        return render_template("inventory.html", products=products)
    except Exception as e:
        return f"Database Error: {str(e)}"


# ------------------------- Session + Cart APIs -------------------------
@app.route("/verify_pin", methods=["POST"])
def verify_pin():
    data = request.json or {}
    label = data.get("cart_label")
    entered_pin = data.get("pin")
    if not label or not entered_pin:
        return jsonify({"status": "error", "message": "Missing cart_label or pin"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT cart_id, pin FROM CART WHERE cart_label = %s",
            (label,),
        )
        cart = cursor.fetchone()
        if not cart:
            conn.close()
            return jsonify({"status": "error", "message": "Cart not found!"}), 404
        if cart["pin"] != entered_pin:
            conn.close()
            return jsonify({"status": "error", "message": "Wrong PIN!"}), 401

        cursor.execute(
            "SELECT session_id FROM SHOPPING_SESSION WHERE cart_id = %s AND status = 'active'",
            (cart["cart_id"],),
        )
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO SHOPPING_SESSION (cart_id, status, total_cost) VALUES (%s, 'active', 0)",
                (cart["cart_id"],),
            )
            conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/get_cart/<label>")
def get_cart_data(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT p.name, p.unit_price, p.barcode, b.quantity
            FROM CART_ITEM_BRIDGE b
            JOIN PRODUCT p ON p.product_id = b.product_id
            JOIN SHOPPING_SESSION s ON s.session_id = b.session_id
            JOIN CART c ON c.cart_id = s.cart_id
            WHERE c.cart_label = %s AND s.status = 'active'
            ORDER BY p.name ASC
            """,
            (label,),
        )
        items = cursor.fetchall()
        conn.close()
        total = sum(float(item["unit_price"]) * int(item["quantity"]) for item in items)
        return jsonify({"items": items, "total": float(total)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/end_session/<label>", methods=["POST"])
def end_session(label):
    if request.headers.get("X-Cashier-Action") != "true":
        return jsonify({"status": "error", "message": "Cashier approval required"}), 403
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE SHOPPING_SESSION s
            JOIN CART c ON c.cart_id = s.cart_id
            SET s.status = 'completed'
            WHERE c.cart_label = %s AND s.status = 'active'
            """,
            (label,),
        )
        cursor.execute("UPDATE CART SET pin = NULL WHERE cart_label = %s", (label,))
        conn.commit()
        conn.close()
        checkout_requests.pop(label, None)
        set_hw_state(label, placement=False, removal=False, expected_weight=0.0, alert=False)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/request_checkout/<label>", methods=["POST"])
def request_checkout(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        session = active_session_for_label(cursor, label)
        conn.close()
        if not session:
            return jsonify({"status": "error", "message": "No active session"}), 404

        checkout_requests[label] = True
        set_hw_state(label, placement=False, removal=False, expected_weight=0.0, alert=False)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/all_carts_status")
def all_carts_status():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT c.cart_label,
                   c.pin,
                   IFNULL(s.status, 'idle') AS status,
                   IFNULL(s.total_cost, 0) AS total_cost
            FROM CART c
            LEFT JOIN SHOPPING_SESSION s
              ON s.cart_id = c.cart_id
             AND s.status = 'active'
            ORDER BY c.cart_label ASC
            """
        )
        data = cursor.fetchall()
        conn.close()
        for row in data:
            row["checkout_requested"] = checkout_requested(row["cart_label"])
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset_everything", methods=["POST"])
def reset_everything():
    if request.headers.get("X-Cashier-Action") != "true":
        return jsonify({"status": "error", "message": "Cashier approval required"}), 403
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE SHOPPING_SESSION SET status = 'completed' WHERE status = 'active'")
        cursor.execute("UPDATE CART SET pin = NULL")
        conn.commit()
        conn.close()

        pending_placement.clear()
        pending_removal.clear()
        security_alerts.clear()
        expected_weight_change.clear()
        checkout_requests.clear()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ------------------------- Product APIs -------------------------
@app.route("/api/get_product_info/<barcode>")
def get_product_info(barcode):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT name, unit_price, weight FROM PRODUCT WHERE barcode = %s",
            (barcode,),
        )
        product = cursor.fetchone()
        conn.close()
        if not product:
            return jsonify({"status": "error", "message": "Product not found"}), 404
        return jsonify(
            {
                "status": "success",
                "name": product["name"],
                "price": float(product["unit_price"]),
                "weight": float(product["weight"]),
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/add_product", methods=["POST"])
def add_product():
    data = request.json or {}
    try:
        item_weight = float(data.get("weight", 0))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO PRODUCT (name, barcode, unit_price, stock_quantity, expected_weight, weight)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                data["name"],
                data["barcode"],
                data["price"],
                data["stock"],
                item_weight,
                item_weight,
            ),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/update_product", methods=["POST"])
def update_product():
    data = request.json or {}
    try:
        item_weight = float(data.get("weight", 0))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE PRODUCT
            SET name = %s, barcode = %s, unit_price = %s, stock_quantity = %s,
                expected_weight = %s, weight = %s
            WHERE product_id = %s
            """,
            (
                data["name"],
                data["barcode"],
                data["price"],
                data["stock"],
                item_weight,
                item_weight,
                data["id"],
            ),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/delete_product/<int:product_id>", methods=["DELETE"])
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


# ------------------------- Scan + Inventory updates -------------------------
@app.route("/scan", methods=["POST"])
def scan_item():
    data = request.json or {}
    barcode = data.get("barcode")
    label = data.get("cart_label")
    if not barcode or not label:
        return jsonify({"status": "error", "message": "Missing barcode or cart_label"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        product = product_by_barcode(cursor, barcode)
        if not product:
            conn.close()
            return jsonify({"status": "error", "message": "Product not found"}), 404
        if int(product["stock_quantity"]) <= 0:
            conn.close()
            return jsonify({"status": "error", "message": "Out of stock"}), 400

        session = active_session_for_label(cursor, label)
        if not session:
            conn.close()
            return jsonify({"status": "error", "message": "No active session"}), 404

        session_id = session["session_id"]
        cursor.execute(
            """
            INSERT INTO CART_ITEM_BRIDGE (session_id, product_id, quantity)
            VALUES (%s, %s, 1)
            ON DUPLICATE KEY UPDATE quantity = quantity + 1
            """,
            (session_id, product["product_id"]),
        )
        cursor.execute(
            "UPDATE SHOPPING_SESSION SET total_cost = total_cost + %s WHERE session_id = %s",
            (product["unit_price"], session_id),
        )
        cursor.execute(
            "UPDATE PRODUCT SET stock_quantity = stock_quantity - 1 WHERE product_id = %s",
            (product["product_id"],),
        )

        set_hw_state(
            label,
            placement=True,
            removal=False,
            expected_weight=product["weight"],
            alert=False,
        )

        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "name": product["name"],
                "weight": float(product["weight"]),
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/remove_item", methods=["POST"])
def remove_item():
    data = request.json or {}
    barcode = data.get("barcode")
    label = data.get("cart_label")
    if not barcode or not label:
        return jsonify({"status": "error", "message": "Missing barcode or cart_label"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        product = product_by_barcode(cursor, barcode)
        session = active_session_for_label(cursor, label)
        if not product or not session:
            conn.close()
            return jsonify({"status": "error", "message": "Item/session not found"}), 404

        session_id = session["session_id"]
        cursor.execute(
            """
            UPDATE CART_ITEM_BRIDGE
            SET quantity = quantity - 1
            WHERE session_id = %s AND product_id = %s
            """,
            (session_id, product["product_id"]),
        )
        cursor.execute("DELETE FROM CART_ITEM_BRIDGE WHERE quantity <= 0")
        cursor.execute(
            "UPDATE SHOPPING_SESSION SET total_cost = total_cost - %s WHERE session_id = %s",
            (product["unit_price"], session_id),
        )

        set_hw_state(
            label,
            placement=False,
            removal=True,
            expected_weight=product["weight"],
        )

        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ------------------------- PIN + hardware APIs -------------------------
@app.route("/api/get_pin/<label>")
def get_pin(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT pin FROM CART WHERE cart_label = %s", (label,))
        row = cursor.fetchone()
        conn.close()
        return jsonify({"pin": row["pin"] if row and row["pin"] else "WAIT"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/placement_status/<label>", methods=["POST"])
def set_placement_status(label):
    set_hw_state(label, placement=True)
    return jsonify({"status": "pending_placement_set"})


@app.route("/api/report_alert/<label>", methods=["POST"])
def report_alert(label):
    set_hw_state(label, alert=True)
    print(f"!!! SECURITY ALERT ON CART {label} !!!")
    return jsonify({"status": "alert_received"})


@app.route("/api/check_alert/<label>")
def check_alert(label):
    return jsonify({"alert": hw_state(label)["alert"]})


@app.route("/api/clear_alert/<label>", methods=["POST"])
def clear_alert(label):
    set_hw_state(label, alert=False)
    return jsonify({"status": "cleared"})


@app.route("/api/confirm_placement/<label>", methods=["POST"])
def confirm_placement(label):
    set_hw_state(label, placement=False, expected_weight=0.0, alert=False)
    return jsonify({"status": "verified"})


@app.route("/api/confirm_removal/<label>", methods=["POST"])
def confirm_removal(label):
    set_hw_state(label, removal=False, expected_weight=0.0)
    return jsonify({"status": "verified"})


@app.route("/api/hardware_state/<label>")
def hardware_state(label):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        session = active_session_for_label(cursor, label)
        total_cost = 0.0
        if session:
            cursor.execute(
                "SELECT total_cost FROM SHOPPING_SESSION WHERE session_id = %s",
                (session["session_id"],),
            )
            total_row = cursor.fetchone()
            total_cost = float(total_row["total_cost"] or 0.0) if total_row else 0.0
        conn.close()
        state = hw_state(label)
        return jsonify(
            {
                "status": session["status"] if session else "idle",
                "checkout_requested": checkout_requested(label),
                "total_cost": total_cost,
                "pending_placement": state["pending_placement"],
                "pending_removal": state["pending_removal"],
                "expected_weight_change": state["expected_weight_change"],
                "alert": state["alert"],
            }
        )
    except Exception as e:
        return jsonify(
            {
                "status": "error",
                "checkout_requested": False,
                "total_cost": 0.0,
                "pending_placement": False,
                "pending_removal": False,
                "expected_weight_change": 0.0,
                "alert": False,
                "message": str(e),
            }
        ), 500


@app.route("/api/cart_update/<label>")
def cart_update(label):
    # Backward-compatible payload for older hardware: status|pendingPlacement|pendingRemoval|weight
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        session = active_session_for_label(cursor, label)
        conn.close()
        state = hw_state(label)
        status = session["status"] if session else "idle"
        return (
            f"{status}|"
            f"{str(state['pending_placement']).lower()}|"
            f"{str(state['pending_removal']).lower()}|"
            f"{state['expected_weight_change']:.1f}"
        )
    except Exception:
        return "error|false|false|0.0"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
