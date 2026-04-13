from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
import logging
import mysql.connector
import os
import random
import re
import time
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "smartcart-secret-key"

# R6 - Cookie hardening: set explicit session cookie controls to reduce CSRF/cookie abuse risk.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SMARTCART_SESSION_COOKIE_SECURE", "0") == "1"

# R6 - CSRF protection: enable Flask-WTF CSRF and enforce it on cashier/inventory forms.
app.config["WTF_CSRF_CHECK_DEFAULT"] = False
csrf = CSRFProtect(app)

app.logger.setLevel(logging.INFO)

db_config = {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "smartcart",
    "password": "smartcartpass",
    "database": "smart_cart_system",
}

DEFAULT_CASHIER_USERNAME = "Cashier"
DEFAULT_CASHIER_PASSWORD = "Cashier@123"
DEFAULT_INVENTORY_USERNAME = "Inventory"
DEFAULT_INVENTORY_PASSWORD = "Inventory@123"

DEFAULT_ROLE_ACCOUNTS = {
    "cashier": {
        "username": DEFAULT_CASHIER_USERNAME,
        "password": DEFAULT_CASHIER_PASSWORD,
    },
    "inventory": {
        "username": DEFAULT_INVENTORY_USERNAME,
        "password": DEFAULT_INVENTORY_PASSWORD,
    },
}

PASSWORD_UPPERCASE_PATTERN = re.compile(r"[A-Z]")
PASSWORD_DIGIT_PATTERN = re.compile(r"\d")
PASSWORD_SYMBOL_PATTERN = re.compile(r"[^A-Za-z0-9]")

# In-memory bridge between web + hardware validation
pending_placement = {}
pending_removal = {}
security_alerts = {}
expected_weight_change = {}
checkout_requests = {}

BARCODE_PATTERN = re.compile(r"^\d{3,14}$")

MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
login_rate_state = {}

TRUSTED_ORIGINS = {
    origin.strip()
    for origin in os.getenv(
        "SMARTCART_TRUSTED_ORIGINS",
        "https://127.0.0.1:5050,http://127.0.0.1:5050,https://localhost:5050,http://localhost:5050",
    ).split(",")
    if origin.strip()
}
HARDWARE_API_KEY = os.getenv("SMARTCART_HARDWARE_API_KEY", "smartcart-hw-key")


# Logs private error details for diagnostics without exposing sensitive internals.
def log_private_error(context, exc):
    app.logger.exception("%s: %s", context, exc)


# Returns whether staff session active is currently true for this request.
def staff_session_active():
    return bool(session.get("cashier_authenticated") or session.get("inventory_authenticated"))


# Runs the customer session active for label routine for this module.
def customer_session_active_for_label(label):
    return session.get("verified_cart_label") == label


# Returns whether hardware key valid passes validation checks.
def hardware_key_valid():
    return request.headers.get("X-Hardware-Key") == HARDWARE_API_KEY


# Returns whether cart access authorized is allowed for the current caller.
def cart_access_authorized(label):
    return bool(
        customer_session_active_for_label(label)
        or staff_session_active()
        or hardware_key_valid()
    )


# Runs the login attempt key routine for this module.
def login_attempt_key(role, username):
    normalized = (username or "").strip().lower() or "unknown"
    ip_address = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    return f"{role}:{normalized}:{ip_address}"


# Checks login lockout and reports the current status.
def check_login_lockout(key):
    state = login_rate_state.get(key)
    if not state:
        return False, 0
    locked_until = state.get("locked_until", 0)
    now = time.time()
    if locked_until > now:
        return True, int(locked_until - now)
    if locked_until:
        login_rate_state.pop(key, None)
    return False, 0


# Records login failure for tracking and later enforcement.
def register_login_failure(key):
    state = login_rate_state.get(key, {"failures": 0, "locked_until": 0})
    state["failures"] = int(state.get("failures", 0)) + 1
    if state["failures"] >= MAX_LOGIN_FAILURES:
        state["locked_until"] = time.time() + LOGIN_LOCK_SECONDS
    login_rate_state[key] = state


# Clears login failures to reset related workflow flags.
def clear_login_failures(key):
    login_rate_state.pop(key, None)


# Returns whether trusted origin meets the required condition.
def is_trusted_origin(origin):
    return bool(origin and origin in TRUSTED_ORIGINS)


# Builds and returns the current request origin value.
def current_request_origin():
    # Build the canonical origin of the current request (scheme + host:port).
    return f"{request.scheme}://{request.host}"


# Returns whether allowed origin meets the required condition.
def is_allowed_origin(origin):
    # Always allow same-origin requests; only enforce TRUSTED_ORIGINS for cross-origin calls.
    return bool(origin and (origin == current_request_origin() or is_trusted_origin(origin)))


# Enforces csrf and cors rules for request safety and consistency.
@app.before_request
def enforce_csrf_and_cors():
    origin = request.headers.get("Origin")
    cors_protected_path = request.path.startswith("/api/") or request.path in {"/scan", "/verify_pin"}

    # R1/R3 - CORS hardening: reject untrusted cross-origin requests for API-style endpoints only.
    if cors_protected_path and origin and not is_allowed_origin(origin):
        return jsonify({"status": "error", "message": "Origin not allowed"}), 403

    # R6 - CSRF enforcement: protect cashier and inventory form POST routes.
    if request.method == "POST" and request.endpoint in {
        "cashier_login",
        "inventory_login",
        "change_admin_password",
    }:
        csrf.protect()


# Applies security headers to the current response or runtime flow.
@app.after_request
def apply_security_headers(response):
    # R7 - CSP header: only allow trusted sources for active content.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    # R8 - Anti-clickjacking: prevent rendering inside iframes.
    response.headers["X-Frame-Options"] = "DENY"
    # R7 - Content sniffing hardening for browsers.
    response.headers["X-Content-Type-Options"] = "nosniff"
    # R7 - Transport hardening header for HTTPS deployments.
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # R7 - Minimize referrer leakage to external sites.
    response.headers["Referrer-Policy"] = "no-referrer"
    # R9 - Server fingerprinting reduction: overwrite framework-identifying server header.
    response.headers["Server"] = "SmartCart"

    origin = request.headers.get("Origin")
    if is_allowed_origin(origin):
        # R1/R3 - CORS restriction: only trusted origins receive CORS allow headers.
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Cashier-Action,X-CSRFToken,X-Hardware-Key"
        response.headers["Vary"] = "Origin"

    return response


# Handles csrf error workflow logic and related state transitions.
@app.errorhandler(CSRFError)
def handle_csrf_error(_err):
    # R6 - CSRF validation failure handling: reject forged form submissions with a safe message.
    if request.path == "/cashier":
        return render_template("cashier_landing.html", login_error="Invalid or missing CSRF token."), 400
    if request.path == "/admin/inventory":
        return render_template("inventory_landing.html", login_error="Invalid or missing CSRF token."), 400
    if request.path == "/admin/change-password":
        return render_template(
            "change_password.html",
            auth_username=DEFAULT_CASHIER_USERNAME,
            error="Invalid or missing CSRF token.",
            success=None,
        ), 400
    return jsonify({"status": "error", "message": "Invalid CSRF token"}), 400


# Retrieves db connection and returns it to the caller.
def get_db_connection():
    return mysql.connector.connect(**db_config)


# Generates pin for the next workflow step.
def generate_pin():
    return str(random.randint(1000, 9999))


# Runs the active session for label routine for this module.
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


# Runs the barcode lookup candidates routine for this module.
def barcode_lookup_candidates(barcode):
    normalized = (barcode or "").strip()
    if not normalized:
        return []

    candidates = [normalized]
    # Many scanners emit UPC-A as EAN-13 with a leading zero and vice versa.
    if normalized.isdigit():
        if len(normalized) == 12:
            candidates.append(f"0{normalized}")
        elif len(normalized) == 13 and normalized.startswith("0"):
            candidates.append(normalized[1:])

    # De-duplicate while preserving order.
    return list(dict.fromkeys(candidates))


# Runs the product by barcode routine for this module.
def product_by_barcode(cursor, barcode):
    for candidate in barcode_lookup_candidates(barcode):
        cursor.execute(
            """
            SELECT product_id, name, unit_price, stock_quantity, weight
            FROM PRODUCT
            WHERE barcode = %s
            """,
            (candidate,),
        )
        product = cursor.fetchone()
        if product:
            return product
    return None


# Returns whether valid scan barcode meets the required condition.
def is_valid_scan_barcode(barcode):
    normalized_barcode = (barcode or "").strip()
    return bool(BARCODE_PATTERN.fullmatch(normalized_barcode)), normalized_barcode


# Validates product identity against expected rules before proceeding.
def validate_product_identity(name, barcode):
    normalized_name = (name or "").strip()
    normalized_barcode = (barcode or "").strip()
    if not normalized_name or not normalized_barcode:
        return False, "Name and barcode are required."
    if normalized_name == normalized_barcode:
        return False, "Product name and barcode cannot be the same."
    if not BARCODE_PATTERN.fullmatch(normalized_barcode):
        return False, "Barcode must be 3 to 14 digits."
    return True, None


# Updates hw state in shared state for subsequent operations.
def set_hw_state(label, placement=None, removal=None, expected_weight=None, alert=None):
    if placement is not None:
        pending_placement[label] = bool(placement)
    if removal is not None:
        pending_removal[label] = bool(removal)
    if expected_weight is not None:
        expected_weight_change[label] = float(expected_weight)
    if alert is not None:
        security_alerts[label] = bool(alert)


# Runs the hw state routine for this module.
def hw_state(label):
    return {
        "pending_placement": pending_placement.get(label, False),
        "pending_removal": pending_removal.get(label, False),
        "expected_weight_change": float(expected_weight_change.get(label, 0.0)),
        "alert": security_alerts.get(label, False),
    }


# Runs the checkout requested routine for this module.
def checkout_requested(label):
    return checkout_requests.get(label, False)


# Ensures default accounts exist is ready before continuing.
def ensure_default_accounts_exist():
    conn = get_db_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ADMIN_ACCOUNT (
                admin_id INT(11) NOT NULL AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )

        for account in DEFAULT_ROLE_ACCOUNTS.values():
            cursor.execute(
                "SELECT admin_id FROM ADMIN_ACCOUNT WHERE username = %s LIMIT 1",
                (account["username"],),
            )
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO ADMIN_ACCOUNT (username, password_hash) VALUES (%s, %s)",
                    (
                        account["username"],
                        generate_password_hash(account["password"]),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


# Validates password strength against expected rules before proceeding.
def validate_password_strength(password):
    candidate = password or ""
    if len(candidate) < 8:
        return False, "Password must be at least 8 characters long."
    if not PASSWORD_UPPERCASE_PATTERN.search(candidate):
        return False, "Password must include at least one uppercase letter."
    if not PASSWORD_DIGIT_PATTERN.search(candidate):
        return False, "Password must include at least one number."
    if not PASSWORD_SYMBOL_PATTERN.search(candidate):
        return False, "Password must include at least one symbol."
    return True, None


# Verifies admin credentials and returns whether it is trusted or correct.
def verify_admin_credentials(username, password, role=None):
    normalized_username = (username or "").strip()
    if not normalized_username:
        return False

    if role:
        default_account = DEFAULT_ROLE_ACCOUNTS.get(role)
        if not default_account:
            return False
        if normalized_username.lower() != default_account["username"].lower():
            return False

    ensure_default_accounts_exist()
    conn = get_db_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT password_hash FROM ADMIN_ACCOUNT WHERE username = %s LIMIT 1",
            (normalized_username,),
        )
        user = cursor.fetchone()
        if not user:
            return False
        return check_password_hash(user["password_hash"], password or "")
    finally:
        conn.close()


# Updates admin password using the latest validated data.
def update_admin_password(username, new_password):
    normalized_username = (username or "").strip()
    if not normalized_username:
        return False

    ensure_default_accounts_exist()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ADMIN_ACCOUNT SET password_hash = %s WHERE username = %s",
            (generate_password_hash(new_password), normalized_username),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ------------------------- Pages -------------------------
# Runs the landing page routine for this module.
@app.route("/")
def landing_page():
    return render_template("landing.html")


# Runs the cashier landing page routine for this module.
@app.route("/cashier")
def cashier_landing_page():
    session.pop("cashier_authenticated", None)
    return render_template("cashier_landing.html", login_error=None)


# Runs the cashier login routine for this module.
@app.route("/cashier", methods=["POST"])
def cashier_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    # R10 - Brute force defense: lock login attempts temporarily after repeated failures.
    attempt_key = login_attempt_key("cashier", username)
    locked, seconds_left = check_login_lockout(attempt_key)
    if locked:
        return render_template(
            "cashier_landing.html",
            login_error=f"Too many failed attempts. Try again in {seconds_left} seconds.",
        ), 429

    if verify_admin_credentials(username, password, role="cashier"):
        clear_login_failures(attempt_key)
        session["cashier_authenticated"] = True
        session["auth_username"] = username
        return redirect(url_for("cashier_page"))

    register_login_failure(attempt_key)
    return render_template(
        "cashier_landing.html",
        login_error="Invalid username or password.",
    ), 401


# Runs the inventory landing page routine for this module.
@app.route("/admin/inventory")
def inventory_landing_page():
    session.pop("inventory_authenticated", None)
    return render_template("inventory_landing.html", login_error=None)


# Runs the inventory login routine for this module.
@app.route("/admin/inventory", methods=["POST"])
def inventory_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    # R10 - Brute force defense: lock login attempts temporarily after repeated failures.
    attempt_key = login_attempt_key("inventory", username)
    locked, seconds_left = check_login_lockout(attempt_key)
    if locked:
        return render_template(
            "inventory_landing.html",
            login_error=f"Too many failed attempts. Try again in {seconds_left} seconds.",
        ), 429

    if verify_admin_credentials(username, password, role="inventory"):
        clear_login_failures(attempt_key)
        session["inventory_authenticated"] = True
        session["auth_username"] = username
        return redirect(url_for("admin_inventory"))

    register_login_failure(attempt_key)
    return render_template(
        "inventory_landing.html",
        login_error="Invalid username or password.",
    ), 401


# Runs the change admin password routine for this module.
@app.route("/admin/change-password", methods=["GET", "POST"])
def change_admin_password():
    try:
        ensure_default_accounts_exist()
    except Exception as e:
        # R10 - Error handling hardening: log internal details privately and return a generic message.
        log_private_error("change_admin_password.ensure_default_accounts_exist", e)
        return "Internal server error", 500

    error = None
    success = None
    username_value = DEFAULT_CASHIER_USERNAME
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        username_value = username
        old_password = request.form.get("old_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username:
            error = "Username is required."
        elif not verify_admin_credentials(username, old_password):
            error = "Old password is incorrect."
        elif new_password != confirm_password:
            error = "New password and confirm password do not match."
        elif old_password == new_password:
            error = "New password must be different from old password."
        else:
            strong_password, strength_error = validate_password_strength(new_password)
            if not strong_password:
                error = strength_error
            elif not update_admin_password(username, new_password):
                error = "Unable to update password. Please try again."
            else:
                success = "Password updated successfully."

    return render_template(
        "change_password.html",
        auth_username=username_value,
        error=error,
        success=success,
    )


# Automatically assigns cart to keep the flow moving.
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
            return render_template("all_carts_busy.html"), 200

        pin = generate_pin()
        cursor.execute(
            "UPDATE CART SET pin = %s WHERE cart_id = %s",
            (pin, available["cart_id"]),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("pin_page", label=available["cart_label"]))
    except Exception as e:
        # R10 - Error handling hardening: log internal details privately and return a generic message.
        log_private_error("auto_assign_cart", e)
        return "Internal server error", 500


# Runs the pin page routine for this module.
@app.route("/pin/<label>")
def pin_page(label):
    return render_template("pin.html", cart_label=label)


# Shows cart in the UI based on current conditions.
@app.route("/cart/<label>")
def show_cart(label):
    if session.get("verified_cart_label") != label:
        return redirect(url_for("pin_page", label=label))
    return render_template("cart.html", cart_label=label)


# Shows bill in the UI based on current conditions.
@app.route("/bill/<label>")
def show_bill(label):
    return render_template("bill.html", cart_label=label)


# Runs the cashier page routine for this module.
@app.route("/cashier/dashboard")
def cashier_page():
    # R4 - Session check: redirect to cashier login when no cashier session exists.
    if not session.get("cashier_authenticated"):
        return redirect(url_for("cashier_landing_page"))
    return render_template("cashier.html")


# Runs the success page routine for this module.
@app.route("/success")
def success_page():
    return render_template("success.html", cart_label=request.args.get("label", ""))


# Runs the admin inventory routine for this module.
@app.route("/admin/inventory/dashboard")
def admin_inventory():
    # R5 - Session check: redirect to inventory login when no inventory session exists.
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
        # R10 - Error handling hardening: log internal details privately and return a generic message.
        log_private_error("admin_inventory", e)
        return "Internal server error", 500


# ------------------------- Session + Cart APIs -------------------------
# Verifies pin and returns whether it is trusted or correct.
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
        # Hide PIN immediately after successful verification.
        cursor.execute("UPDATE CART SET pin = NULL WHERE cart_id = %s", (cart["cart_id"],))
        conn.commit()
        conn.close()
        session["verified_cart_label"] = label
        return jsonify({"status": "success"})
    except Exception as e:
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("verify_pin", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Retrieves cart data and returns it to the caller.
@app.route("/api/get_cart/<label>")
def get_cart_data(label):
    # R3 - Cart access control: require a valid customer/staff session before returning cart contents.
    if not cart_access_authorized(label):
        return jsonify({"status": "error", "message": "Authentication required"}), 401

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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("get_cart_data", e)
        return jsonify({"error": "Internal server error"}), 500


# Ends session and performs required cleanup actions.
@app.route("/api/end_session/<label>", methods=["POST"])
def end_session(label):
    # R4 - Session check: only authenticated cashier sessions can close sessions.
    if not session.get("cashier_authenticated"):
        return jsonify({"status": "error", "message": "Authentication required"}), 401
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("end_session", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Requests checkout from the server and handles the response.
@app.route("/api/request_checkout/<label>", methods=["POST"])
def request_checkout(label):
    # R1 - Session check: require a valid customer session before checkout request is accepted.
    if not customer_session_active_for_label(label):
        return jsonify({"status": "error", "message": "Authentication required"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        current_session = active_session_for_label(cursor, label)
        conn.close()
        if not current_session:
            return jsonify({"status": "error", "message": "No active session"}), 404

        checkout_requests[label] = True
        set_hw_state(label, placement=False, removal=False, expected_weight=0.0, alert=False)
        return jsonify({"status": "success"})
    except Exception as e:
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("request_checkout", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Runs the all carts status routine for this module.
@app.route("/api/all_carts_status")
def all_carts_status():
    # R2 - Cart status access control: only authenticated staff sessions can list all carts.
    if not staff_session_active():
        return jsonify({"status": "error", "message": "Authentication required"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT c.cart_label,
                   c.pin AS display_code,
                   CASE
                       WHEN s.status = 'active' THEN 'active'
                       WHEN c.pin IS NOT NULL THEN 'awaiting_pin'
                       ELSE 'idle'
                   END AS status,
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
            label = row["cart_label"]
            row["checkout_requested"] = checkout_requested(label)
            state = hw_state(label)
            row["verification_alert"] = bool(state["alert"])
            if not row.get("display_code"):
                row["display_code"] = "WAIT"
        return jsonify(data)
    except Exception as e:
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("all_carts_status", e)
        return jsonify({"error": "Internal server error"}), 500


# Resets everything back to a known baseline state.
@app.route("/api/reset_everything", methods=["POST"])
def reset_everything():
    # R4 - Session check: only authenticated cashier sessions can reset all carts.
    if not session.get("cashier_authenticated"):
        return jsonify({"status": "error", "message": "Authentication required"}), 401
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("reset_everything", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# ------------------------- Product APIs -------------------------
# Retrieves product info and returns it to the caller.
@app.route("/api/get_product_info/<barcode>")
def get_product_info(barcode):
    try:
        is_valid, normalized_barcode = is_valid_scan_barcode(barcode)
        if not is_valid:
            return jsonify({"status": "error", "message": "Invalid barcode format"}), 400

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        product = product_by_barcode(cursor, normalized_barcode)
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("get_product_info", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Adds product to the active collection or session.
@app.route("/api/add_product", methods=["POST"])
def add_product():
    data = request.json or {}
    try:
        is_valid, error_message = validate_product_identity(data.get("name"), data.get("barcode"))
        if not is_valid:
            return jsonify({"status": "error", "message": error_message}), 400

        item_weight = float(data.get("weight", 0))
        normalized_barcode = data.get("barcode", "").strip()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO PRODUCT (name, barcode, unit_price, stock_quantity, expected_weight, weight)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                data["name"],
                normalized_barcode,
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("add_product", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Updates product using the latest validated data.
@app.route("/api/update_product", methods=["POST"])
def update_product():
    data = request.json or {}
    try:
        is_valid, error_message = validate_product_identity(data.get("name"), data.get("barcode"))
        if not is_valid:
            return jsonify({"status": "error", "message": error_message}), 400

        item_weight = float(data.get("weight", 0))
        normalized_barcode = data.get("barcode", "").strip()
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
                normalized_barcode,
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("update_product", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Deletes product and cleans up related records.
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("delete_product", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# ------------------------- Scan + Inventory updates -------------------------
# Runs the scan item routine for this module.
@app.route("/scan", methods=["POST"])
def scan_item():
    data = request.json or {}
    barcode = data.get("barcode")
    label = data.get("cart_label")
    if not barcode or not label:
        return jsonify({"status": "error", "message": "Missing barcode or cart_label"}), 400

    is_valid, normalized_barcode = is_valid_scan_barcode(barcode)
    if not is_valid:
        return jsonify({"status": "error", "message": "Invalid barcode format"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        product = product_by_barcode(cursor, normalized_barcode)
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("scan_item", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# Removes item from the active collection or session.
@app.route("/api/remove_item", methods=["POST"])
def remove_item():
    data = request.json or {}
    barcode = data.get("barcode")
    label = data.get("cart_label")
    if not barcode or not label:
        return jsonify({"status": "error", "message": "Missing barcode or cart_label"}), 400

    is_valid, normalized_barcode = is_valid_scan_barcode(barcode)
    if not is_valid:
        return jsonify({"status": "error", "message": "Invalid barcode format"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        product = product_by_barcode(cursor, normalized_barcode)
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
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("remove_item", e)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


# ------------------------- PIN + hardware APIs -------------------------
# Retrieves pin and returns it to the caller.
@app.route("/api/get_pin/<label>")
def get_pin(label):
    # R2 - PIN exposure prevention: never return the PIN field in any API response.
    if not hardware_key_valid():
        return jsonify({"status": "error", "message": "Authentication required"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT pin FROM CART WHERE cart_label = %s", (label,))
        row = cursor.fetchone()
        conn.close()
        return jsonify({"display_code": row["pin"] if row and row["pin"] else "WAIT"})
    except Exception as e:
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("get_pin", e)
        return jsonify({"error": "Internal server error"}), 500


# Updates placement status in shared state for subsequent operations.
@app.route("/api/placement_status/<label>", methods=["POST"])
def set_placement_status(label):
    set_hw_state(label, placement=True)
    return jsonify({"status": "pending_placement_set"})


# Runs the report alert routine for this module.
@app.route("/api/report_alert/<label>", methods=["POST"])
def report_alert(label):
    set_hw_state(label, alert=True)
    return jsonify({"status": "alert_received"})


# Checks alert and reports the current status.
@app.route("/api/check_alert/<label>")
def check_alert(label):
    return jsonify({"alert": hw_state(label)["alert"]})


# Clears alert to reset related workflow flags.
@app.route("/api/clear_alert/<label>", methods=["POST"])
def clear_alert(label):
    set_hw_state(label, alert=False)
    return jsonify({"status": "cleared"})


# Confirms placement and clears pending verification state.
@app.route("/api/confirm_placement/<label>", methods=["POST"])
def confirm_placement(label):
    set_hw_state(label, placement=False, expected_weight=0.0, alert=False)
    return jsonify({"status": "verified"})


# Confirms removal and clears pending verification state.
@app.route("/api/confirm_removal/<label>", methods=["POST"])
def confirm_removal(label):
    set_hw_state(label, removal=False, expected_weight=0.0, alert=False)
    return jsonify({"status": "verified"})


# Runs the hardware state routine for this module.
@app.route("/api/hardware_state/<label>")
def hardware_state(label):
    # R1 - Cart state access control: require valid customer/staff session or trusted hardware key.
    if not cart_access_authorized(label):
        return jsonify({"status": "error", "message": "Authentication required"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        current_session = active_session_for_label(cursor, label)
        total_cost = 0.0
        if current_session:
            cursor.execute(
                "SELECT total_cost FROM SHOPPING_SESSION WHERE session_id = %s",
                (current_session["session_id"],),
            )
            total_row = cursor.fetchone()
            total_cost = float(total_row["total_cost"] or 0.0) if total_row else 0.0
        conn.close()
        state = hw_state(label)
        return jsonify(
            {
                "status": current_session["status"] if current_session else "idle",
                "checkout_requested": checkout_requested(label),
                "total_cost": total_cost,
                "pending_placement": state["pending_placement"],
                "pending_removal": state["pending_removal"],
                "expected_weight_change": state["expected_weight_change"],
                "alert": state["alert"],
            }
        )
    except Exception as e:
        # R10 - Error handling hardening: hide internals from API consumers.
        log_private_error("hardware_state", e)
        return jsonify(
            {
                "status": "error",
                "checkout_requested": False,
                "total_cost": 0.0,
                "pending_placement": False,
                "pending_removal": False,
                "expected_weight_change": 0.0,
                "alert": False,
                "message": "Internal server error",
            }
        ), 500


# Runs the cart update routine for this module.
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