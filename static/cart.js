const urlSegments = window.location.pathname.split('/');
const cartLabel = urlSegments[urlSegments.length - 1];
document.getElementById('displayCartLabel').innerText = cartLabel;

let currentScanData = null;
let cartInterval = setInterval(updateCart, 2000);
let securityInterval = setInterval(checkSecurity, 2500);

async function checkSecurity() {
    try {
        const response = await fetch(`/api/check_alert/${cartLabel}`);
        const data = await response.json();
        if (data.alert === true) {
            alert("⚠️ SECURITY ALERT: Unscanned item detected! Please remove the item to continue.");
            await fetch(`/api/clear_alert/${cartLabel}`, { method: 'POST' });
        }
    } catch (err) { console.log("Security check failed"); }
}

async function updateCart() {
    try {
        const response = await fetch(`/api/get_cart/${cartLabel}`);
        const data = await response.json();
        if (data.error) { clearInterval(cartInterval); return; }

        const cartList = document.getElementById('cartList');
        cartList.innerHTML = "";

        if (data.items.length === 0) {
            cartList.innerHTML = "<p style='text-align:center; padding: 20px; color:#888;'>Your cart is empty.</p>";
        }

        data.items.forEach(item => {
            cartList.innerHTML += `
                <div class="cart-item">
                    <div>
                        <p class="item-name">${item.name}</p>
                        <p class="item-qty">Qty: ${item.quantity}</p>
                    </div>
                    <div style="display:flex; flex-direction:column; align-items:flex-end; gap:8px;">
                        <p class="item-price">Rs. ${item.unit_price * item.quantity}</p>
                        <button class="remove-btn" onclick="removeItem('${item.barcode}')">
                            Remove
                        </button>
                    </div>
                </div>`;
        });

        document.getElementById('itemCount').innerText = data.items.length + " items";
        document.getElementById('summarySubtotal').innerText = "Rs. " + data.total;
        document.getElementById('summaryTotal').innerText = "Rs. " + data.total;
    } catch (err) { console.log("Searching for server..."); }
}

async function removeItem(barcode) {
    if (!confirm("Are you sure you want to remove this item?")) return;
    try {
        await fetch('/api/remove_item', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: barcode, cart_label: cartLabel })
        });
        alert("Please physically remove the item from the cart now.");
        updateCart();
    } catch (err) { alert("Error removing item"); }
}

async function onScanSuccess(decodedText) {
    html5QrcodeScanner.pause(true);
    document.getElementById('scan-status').innerText = "Processing scan...";
    try {
        const response = await fetch(`/api/get_product_info/${decodedText}`);
        const product = await response.json();
        if (product.status === "success") {
            currentScanData = decodedText;
            document.getElementById('modalProdName').innerText = product.name;
            document.getElementById('modalProdPrice').innerText = "Rs. " + product.price;
            document.getElementById('scanModal').style.display = "flex";
        } else {
            alert("Error: " + product.message);
            html5QrcodeScanner.resume();
        }
    } catch (err) { html5QrcodeScanner.resume(); }
}

async function confirmAddition() {
    await fetch(`/api/placement_status/${cartLabel}`, { method: 'POST' });
    try {
        const response = await fetch('/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: currentScanData, cart_label: cartLabel })
        });
        const result = await response.json();
        if (result.status === "success") {
            updateCart();
            closeModal();
        } else {
            alert(result.message);
            closeModal();
        }
    } catch (err) { alert("Server Error"); closeModal(); }
}

function closeModal() {
    document.getElementById('scanModal').style.display = "none";
    currentScanData = null;
    document.getElementById('scan-status').innerText = "Camera ready for scanning";
    setTimeout(() => { html5QrcodeScanner.resume(); }, 500);
}

let html5QrcodeScanner = new Html5Qrcode("reader");
html5QrcodeScanner.start({ facingMode: "environment" }, { fps: 20, qrbox: { width: 250, height: 150 } }, onScanSuccess)
.catch(() => {
    html5QrcodeScanner.start({ facingMode: "user" }, { fps: 20, qrbox: { width: 250, height: 150 } }, onScanSuccess);
});

async function endSession() {
    if (confirm("Are you sure you want to finish shopping?")) {
        // Stop all background processes
        clearInterval(cartInterval);
        clearInterval(securityInterval);
        
        try {
            if (html5QrcodeScanner.getState() === 2) { 
                await html5QrcodeScanner.stop();
            }
        } catch (e) { console.log("Camera cleanup"); }

        const response = await fetch(`/api/end_session/${cartLabel}`, { method: 'POST' });
        
        if (response.ok) { 
            // Correct redirection
            window.location.href = "/success"; 
        } else { 
            cartInterval = setInterval(updateCart, 2000);
            securityInterval = setInterval(checkSecurity, 2500);
            alert("Error ending session."); 
        }
    }
}

updateCart();