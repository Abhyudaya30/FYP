const urlSegments = window.location.pathname.split("/");
const cartLabel = urlSegments[urlSegments.length - 1];
document.getElementById("displayCartLabel").innerText = cartLabel;

const supportedLinearFormats = typeof Html5QrcodeSupportedFormats !== "undefined"
    ? [
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.EAN_8,
        Html5QrcodeSupportedFormats.UPC_A,
        Html5QrcodeSupportedFormats.UPC_E,
        Html5QrcodeSupportedFormats.CODE_128,
        Html5QrcodeSupportedFormats.CODE_39,
        Html5QrcodeSupportedFormats.CODABAR,
        Html5QrcodeSupportedFormats.ITF
    ].filter(format => format !== undefined && format !== null)
    : null;
const scannerConfig = {
    fps: 15,
    qrbox: { width: 280, height: 120 },
    ...(supportedLinearFormats ? { formatsToSupport: supportedLinearFormats } : {})
};
const hasQrLib = typeof Html5Qrcode !== "undefined";
const scanner = hasQrLib ? new Html5Qrcode("reader") : null;
const startCameraBtn = document.getElementById("startCameraBtn");
const verificationAlertIcon = document.getElementById("verificationAlertIcon");
let preferredCameraId = null;
let scannerStartInFlight = null;
const scannerStates = hasQrLib && typeof Html5QrcodeScannerState !== "undefined"
    ? Html5QrcodeScannerState
    : { UNKNOWN: 0, NOT_STARTED: 1, SCANNING: 2, PAUSED: 3 };

const state = {
    currentScanData: null,
    scanInProgress: false,
    addInProgress: false,
    securityPopupVisible: false,
    securityPopupAcknowledged: false,
    lastDecodedText: "",
    lastScanAt: 0
};

const barcodePattern = /^\d{3,14}$/;

let cartInterval = setInterval(updateCart, 2000);
let securityInterval = setInterval(checkSecurity, 2500);

async function apiJson(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json();
    return { ok: response.ok, data };
}

function setScanStatus(text) {
    const el = document.getElementById("scan-status");
    if (el) el.innerText = text;
}

function setVerificationAlertState(visible, message) {
    if (!verificationAlertIcon) return;
    verificationAlertIcon.classList.toggle("visible", visible);
    verificationAlertIcon.title = message || "Unverified item in cart";
    verificationAlertIcon.setAttribute("aria-hidden", visible ? "false" : "true");
}

function ensureSecurityAlertPopup() {
    let overlay = document.getElementById("securityAlertOverlay");
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.id = "securityAlertOverlay";
    overlay.style.cssText = [
        "display:none",
        "position:fixed",
        "inset:0",
        "background:rgba(15,23,42,0.55)",
        "z-index:9999",
        "align-items:center",
        "justify-content:center",
        "padding:20px"
    ].join(";");

    overlay.innerHTML = `
        <div style="max-width:360px;width:100%;background:#fff;border-radius:18px;padding:22px;box-shadow:0 20px 60px rgba(0,0,0,0.25);text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#991b1b;margin-bottom:10px;">Security Alert</div>
            <div id="securityAlertText" style="font-size:14px;line-height:1.5;color:#334155;margin-bottom:18px;">
                Unverified item detected. Please verify item placement/removal.
            </div>
            <button id="securityAlertDismissBtn" style="border:none;background:#0f172a;color:#fff;padding:12px 18px;border-radius:10px;font-weight:600;cursor:pointer;">
                OK
            </button>
        </div>
    `;

    document.body.appendChild(overlay);
    document.getElementById("securityAlertDismissBtn").addEventListener("click", () => {
        overlay.style.display = "none";
        state.securityPopupVisible = false;
        state.securityPopupAcknowledged = true;
        if (!modalOpen() && !state.scanInProgress) {
            ensureScannerRunning();
        }
    });

    return overlay;
}

function showSecurityAlertPopup(message) {
    const overlay = ensureSecurityAlertPopup();
    const textEl = document.getElementById("securityAlertText");
    if (textEl && message) {
        textEl.textContent = message;
    }
    overlay.style.display = "flex";
    state.securityPopupVisible = true;
}

function hideSecurityAlertPopup() {
    const overlay = document.getElementById("securityAlertOverlay");
    if (!overlay) return;
    overlay.style.display = "none";
    state.securityPopupVisible = false;
}

function showStartCamera(show) {
    if (!startCameraBtn) return;
    startCameraBtn.style.display = show ? "block" : "none";
}

function setStartCameraBusy(busy) {
    if (!startCameraBtn) return;
    startCameraBtn.disabled = busy;
    startCameraBtn.style.opacity = busy ? "0.7" : "1";
    startCameraBtn.style.cursor = busy ? "not-allowed" : "pointer";
}

function modalOpen() {
    return document.getElementById("scanModal").style.display === "flex";
}

function isSecureContextForCamera() {
    return window.isSecureContext === true || window.location.hostname === "localhost";
}

function isIosFamily() {
    const ua = navigator.userAgent || "";
    return /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

async function requestCameraPermission() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return { granted: false, reason: "unsupported" };
    }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: "environment" } }
        });
        stream.getTracks().forEach(track => track.stop());
        return { granted: true, reason: "" };
    } catch (err) {
        console.log("getUserMedia permission failed", err);
        const reason = err && err.name ? err.name : "unknown";
        return { granted: false, reason };
    }
}

async function getCameraToUse() {
    if (!hasQrLib) return null;
    try {
        const cameras = await Html5Qrcode.getCameras();
        if (!cameras || cameras.length === 0) return null;

        // Prefer back/environment-like camera names when available.
        const rearCamera = cameras.find(c => /back|rear|environment/i.test(c.label || ""));
        return rearCamera ? rearCamera.id : cameras[0].id;
    } catch (err) {
        console.log("Unable to enumerate cameras", err);
        return null;
    }
}

async function ensureScannerRunning() {
    if (!hasQrLib || !scanner) {
        setScanStatus("Scanner library failed to load");
        showStartCamera(false);
        return false;
    }

    if (scannerStartInFlight) return scannerStartInFlight;
    scannerStartInFlight = (async () => {
        setStartCameraBusy(true);
        if (!isSecureContextForCamera()) {
            setScanStatus("Camera requires HTTPS");
            showStartCamera(false);
            return false;
        }

        // If scanner is already running/paused, just keep/resume and return.
        try {
            const scannerState = scanner.getState();
            if (scannerState === scannerStates.SCANNING) {
                setScanStatus("Camera ready for scanning");
                showStartCamera(false);
                return true;
            }
            if (scannerState === scannerStates.PAUSED) {
                scanner.resume();
                setScanStatus("Camera ready for scanning");
                showStartCamera(false);
                return true;
            }
            if (scannerState === scannerStates.NOT_STARTED) {
                setScanStatus("Camera ready for scanning");
            } else if (scannerState === scannerStates.UNKNOWN) {
                setScanStatus("Starting camera...");
            }
        } catch (_) {}

        setScanStatus("Starting camera...");

        // Try permission prompt path first for mobile browsers, but do not block startup on failures.
        const permission = await requestCameraPermission();
        if (!permission.granted) {
            if (permission.reason === "NotAllowedError" || permission.reason === "PermissionDeniedError") {
                setScanStatus("Camera blocked. Allow camera permission in browser settings.");
            } else if (permission.reason === "unsupported") {
                setScanStatus("This browser does not support camera access.");
            }
        }

        if (!preferredCameraId) {
            preferredCameraId = await getCameraToUse();
        }

        try {
            const startCandidates = [];
            if (preferredCameraId) startCandidates.push(preferredCameraId);
            startCandidates.push({ facingMode: { exact: "environment" } });
            startCandidates.push({ facingMode: "environment" });
            startCandidates.push({ facingMode: "user" });

            let startError = null;
            let started = false;

            for (const cameraArg of startCandidates) {
                try {
                    await scanner.start(cameraArg, scannerConfig, onScanSuccess);
                    started = true;
                    break;
                } catch (err) {
                    startError = err;
                }
            }

            if (!started) {
                throw startError || new Error("No camera candidate started");
            }

            await new Promise(r => setTimeout(r, 900));
            const video = document.querySelector("#reader video");
            const videoOk = !!(video && video.videoWidth > 0 && video.videoHeight > 0);
            if (videoOk) {
                setScanStatus("Camera ready for scanning");
                showStartCamera(false);
                return true;
            }

            // If stream still not visible, retry once with the first available camera id.
            if (!preferredCameraId) {
                preferredCameraId = await getCameraToUse();
            }
            if (preferredCameraId) {
                try {
                    await scanner.stop();
                } catch (_) {}
                await scanner.start(preferredCameraId, scannerConfig, onScanSuccess);
                await new Promise(r => setTimeout(r, 900));
                const retryVideo = document.querySelector("#reader video");
                const retryOk = !!(retryVideo && retryVideo.videoWidth > 0 && retryVideo.videoHeight > 0);
                if (retryOk) {
                    setScanStatus("Camera ready for scanning");
                    showStartCamera(false);
                    return true;
                }
            }
        } catch (err) {
            console.log("Unable to start scanner", err);
            const errName = err && err.name ? err.name : "";
            if (errName === "NotAllowedError" || errName === "PermissionDeniedError") {
                setScanStatus("Camera blocked. Allow permission and tap again.");
            } else if (errName === "NotFoundError" || errName === "DevicesNotFoundError") {
                setScanStatus("No camera device found.");
            } else if (errName === "NotReadableError" || errName === "TrackStartError") {
                setScanStatus("Camera is busy in another app/tab.");
            }
        }

        if (document.getElementById("scan-status")?.innerText === "Starting camera...") {
            setScanStatus("Unable to start camera");
        }
        showStartCamera(true);
        return false;
    })().finally(() => {
        setStartCameraBusy(false);
        scannerStartInFlight = null;
    });
    return scannerStartInFlight;
}

async function checkSecurity() {
    try {
        const { data } = await apiJson(`/api/hardware_state/${cartLabel}`);
        const hasSecurityAlert = data.alert === true;

        const showAlert = hasSecurityAlert;
        const message = "Security alert: weight mismatch or unscanned item detected";

        setVerificationAlertState(showAlert, message);

        if (showAlert) {
            if (!state.securityPopupVisible && !state.securityPopupAcknowledged) {
                showSecurityAlertPopup(message);
            }
        } else {
            state.securityPopupAcknowledged = false;
            hideSecurityAlertPopup();
        }
    } catch (err) {
        console.log("Security check failed");
    }
}

function renderCart(data) {
    const cartList = document.getElementById("cartList");
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
                    <button class="remove-btn" onclick="removeItem('${item.barcode}')">Remove</button>
                </div>
            </div>`;
    });

    document.getElementById("itemCount").innerText = data.items.length + " items";
    document.getElementById("summarySubtotal").innerText = "Rs. " + data.total;
    document.getElementById("summaryTotal").innerText = "Rs. " + data.total;
}

async function updateCart() {
    try {
        const { data } = await apiJson(`/api/get_cart/${cartLabel}`);
        if (data.error) {
            clearInterval(cartInterval);
            return;
        }
        renderCart(data);
    } catch (err) {
        console.log("Searching for server...");
    }
}

async function removeItem(barcode) {
    if (!confirm("Are you sure you want to remove this item?")) return;
    try {
        await fetch("/api/remove_item", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ barcode, cart_label: cartLabel })
        });
        alert("Please physically remove the item from the cart now.");
        updateCart();
    } catch (err) {
        alert("Error removing item");
    }
}

async function onScanSuccess(decodedText) {
    if (!scanner) return;
    const now = Date.now();
    if (state.scanInProgress) return;
    if (decodedText === state.lastDecodedText && now - state.lastScanAt < 1500) return;

    state.scanInProgress = true;
    state.lastDecodedText = decodedText;
    state.lastScanAt = now;

    const normalizedBarcode = (decodedText || "").trim();
    if (!barcodePattern.test(normalizedBarcode)) {
        setScanStatus("Unrecognized barcode");
        state.scanInProgress = false;
        setTimeout(() => {
            if (!modalOpen()) setScanStatus("Camera ready for scanning");
        }, 1200);
        await ensureScannerRunning();
        return;
    }

    try { scanner.pause(true); } catch (_) {}
    setScanStatus("Processing scan...");

    try {
        const { data } = await apiJson(`/api/get_product_info/${encodeURIComponent(normalizedBarcode)}`);
        if (data.status !== "success") {
            setScanStatus(data.message === "Invalid barcode format" ? "Unrecognized barcode" : "Unknown barcode");
            state.scanInProgress = false;
            setTimeout(() => {
                if (!modalOpen()) setScanStatus("Camera ready for scanning");
            }, 1200);
            await ensureScannerRunning();
            return;
        }

        state.currentScanData = normalizedBarcode;
        document.getElementById("modalProdName").innerText = data.name;
        document.getElementById("modalProdPrice").innerText = "Rs. " + data.price;
        document.getElementById("scanModal").style.display = "flex";
    } catch (err) {
        state.scanInProgress = false;
        await ensureScannerRunning();
    }
}

async function confirmAddition() {
    if (state.addInProgress || !state.currentScanData) return;
    state.addInProgress = true;
    try {
        const { data } = await apiJson("/scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                barcode: state.currentScanData,
                cart_label: cartLabel
            })
        });

        if (data.status === "success") {
            alert("Item added! Please place " + data.name + " in the cart.");
            updateCart();
        } else {
            alert(data.message || "Unable to add item.");
        }
        closeModal();
    } catch (err) {
        alert("Server Error - check connection");
        closeModal();
    } finally {
        state.addInProgress = false;
    }
}

function closeModal() {
    document.getElementById("scanModal").style.display = "none";
    state.currentScanData = null;
    state.scanInProgress = false;
    setScanStatus("Camera ready for scanning");
    setTimeout(ensureScannerRunning, 500);
}

async function endSession() {
    if (!confirm("Are you sure you want to finish shopping?")) return;

    clearInterval(cartInterval);
    clearInterval(securityInterval);

    try {
        if (scanner.getState() === 2) {
            await scanner.stop();
        }
    } catch (err) {
        console.log("Camera cleanup");
    }

    const response = await fetch(`/api/request_checkout/${cartLabel}`, { method: "POST" });
    if (response.ok) {
        window.location.href = `/success?label=${encodeURIComponent(cartLabel)}`;
    } else {
        cartInterval = setInterval(updateCart, 2000);
        securityInterval = setInterval(checkSecurity, 2500);
        alert("Error requesting cashier checkout.");
    }
}

if (startCameraBtn) {
    startCameraBtn.addEventListener("click", async () => {
        setScanStatus("Starting camera...");
        await ensureScannerRunning();
    });
}

// Always try auto-start first so the page does not sit on a manual prompt message.
if (hasQrLib) {
    ensureScannerRunning();
} else {
    setScanStatus("Scanner library failed to load");
}
updateCart();
checkSecurity();
