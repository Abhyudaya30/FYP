#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <LiquidCrystal_I2C.h>
#include <Wire.h>
#include <math.h>
#include "HX711.h"

// --- NETWORK CONFIG ---
const char* SSID = "GEESE";
const char* PASSWORD = "biggreencoat";
const String SERVER_BASE = "https://tiesha-gyroidal-drema.ngrok-free.dev";
const String CART_LABEL = "01";

// --- ESP32-S3 HARDWARE PINS ---
const int LOADCELL_DOUT_PIN = 20;
const int LOADCELL_SCK_PIN = 21;
const int BUZZER_PIN = 5;
const int I2C_SDA_PIN = 8;
const int I2C_SCL_PIN = 9;
const int BUZZER_CHANNEL = 0;
const int BUZZER_FREQUENCY = 2400;
const int BUZZER_RESOLUTION = 8;
const int LCD_COLS = 16;

// --- OBJECTS ---
HX711 scale;
LiquidCrystal_I2C lcd(0x27, 16, 2);
WiFiClientSecure tlsClient;

// --- WEIGHT CONFIG ---
float calibrationFactor = 114.2f;
const float MICRO_SPIKE_BAND = 3.0f;
const float EMPTY_CART_BAND = 10.0f;
const float NEGATIVE_TARE_THRESHOLD = -0.5f;
const float UNVERIFIED_RISE_THRESHOLD = 45.0f;
const float UNVERIFIED_DROP_THRESHOLD = 45.0f;
const float PRODUCT_WEIGHT_TOLERANCE = 0.15f;  // 15%
const float MIN_PRODUCT_TOLERANCE_GRAMS = 25.0f;
const float RANGE_EXIT_GRACE = 8.0f;
const float IDLE_BASELINE_DRIFT_BAND = 8.0f;
const float IDLE_SPIKE_JUMP_THRESHOLD = 180.0f;
const int WEIGHT_SAMPLE_COUNT = 5;

// --- TIMINGS ---
const unsigned long UPDATE_INTERVAL_MS = 350;
const unsigned long PIN_FETCH_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_INTERVAL_MS = 2500;
const unsigned long NEGATIVE_TARE_HOLD_MS = 1200;
const unsigned long EMPTY_AUTO_TARE_MS = 15000;
const unsigned long EXPECTED_WEIGHT_HOLD_MS = 900;
const unsigned long SECURITY_RISE_HOLD_MS = 1200;
const unsigned long EXPECTED_MISMATCH_HOLD_MS = 1800;
const unsigned long IDLE_SPIKE_CONFIRM_MS = 450;

// --- RUNTIME STATE ---
float currentWeight = 0.0f;
float baselineWeight = 0.0f;
float expectedWeight = 0.0f;
float totalCost = 0.0f;

String currentPin = "WAIT";
String cartStatus = "idle";

bool pendingPlacement = false;
bool pendingRemoval = false;
bool securityAlertRaised = false;
bool checkoutPending = false;
bool buzzerReady = false;

unsigned long lastUpdate = 0;
unsigned long lastPinFetch = 0;
unsigned long lastWifiRetry = 0;
unsigned long negativeWeightStart = 0;
unsigned long emptyCartStart = 0;
unsigned long placementStableStart = 0;
unsigned long removalStableStart = 0;
unsigned long placementMismatchStart = 0;
unsigned long removalMismatchStart = 0;
unsigned long unverifiedRiseStart = 0;
unsigned long idleSpikeStart = 0;
float idleSpikeCandidate = 0.0f;
String lastLcdLine1 = "";
String lastLcdLine2 = "";

String fitLcdLine(const String& text) {
    String line = text.substring(0, LCD_COLS);
    while (line.length() < LCD_COLS) {
        line += ' ';
    }
    return line;
}

void resetIdleSpikeTracking() {
    idleSpikeStart = 0;
    idleSpikeCandidate = 0.0f;
}

void resetVerificationTracking() {
    placementStableStart = 0;
    removalStableStart = 0;
    placementMismatchStart = 0;
    removalMismatchStart = 0;
    unverifiedRiseStart = 0;
}

float normalizedExpectedWeight(float rawExpectedWeight) {
    float expectedAbs = fabsf(rawExpectedWeight);
    if (expectedAbs <= 10.0f) {
        expectedAbs *= 1000.0f;
    }
    return expectedAbs;
}

float expectedToleranceWeight(float expectedAbs) {
    return fmaxf(expectedAbs * PRODUCT_WEIGHT_TOLERANCE, MIN_PRODUCT_TOLERANCE_GRAMS);
}

bool mismatchHeldLongEnough(unsigned long& mismatchStart, bool isMismatch) {
    if (!isMismatch) {
        mismatchStart = 0;
        return false;
    }

    unsigned long now = millis();
    if (mismatchStart == 0) {
        mismatchStart = now;
    }

    return (now - mismatchStart) >= EXPECTED_MISMATCH_HOLD_MS;
}

void beep(int durationMs) {
    if (buzzerReady) {
        ledcWriteTone(BUZZER_PIN, BUZZER_FREQUENCY);
        delay(durationMs);
        ledcWriteTone(BUZZER_PIN, 0);
        return;
    }

    digitalWrite(BUZZER_PIN, HIGH);
    delay(durationMs);
    digitalWrite(BUZZER_PIN, LOW);
}

void lcdPrint(const String& line1, const String& line2) {
    String clippedLine1 = fitLcdLine(line1);
    String clippedLine2 = fitLcdLine(line2);

    if (clippedLine1 == lastLcdLine1 && clippedLine2 == lastLcdLine2) {
        return;
    }

    lastLcdLine1 = clippedLine1;
    lastLcdLine2 = clippedLine2;
    lcd.setCursor(0, 0);
    lcd.print(clippedLine1);
    lcd.setCursor(0, 1);
    lcd.print(clippedLine2);
}

void resetScaleTracking() {
    baselineWeight = 0.0f;
    currentWeight = 0.0f;
    negativeWeightStart = 0;
    emptyCartStart = millis();
    resetVerificationTracking();
    resetIdleSpikeTracking();
    securityAlertRaised = false;
}

void tareScale(const char* reason) {
    scale.tare();
    resetScaleTracking();
    Serial.print("Scale tared: ");
    Serial.println(reason);
}

void connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setTxPower(WIFI_POWER_11dBm);
    WiFi.begin(SSID, PASSWORD);
    lcdPrint("Connecting...", "Joining WiFi");

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
        delay(250);
        Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi connected");
    } else {
        Serial.println("\nWiFi timeout");
    }
}

void ensureWiFi() {
    if (WiFi.status() == WL_CONNECTED) return;
    if (millis() - lastWifiRetry < WIFI_RETRY_INTERVAL_MS) return;

    lastWifiRetry = millis();
    WiFi.disconnect();
    WiFi.begin(SSID, PASSWORD);
}

bool postJson(const String& endpoint, const String& body, String& responseBody, int& httpCode) {
    if (WiFi.status() != WL_CONNECTED) {
        httpCode = -1;
        responseBody = "";
        return false;
    }

    HTTPClient http;
    http.setReuse(true);
    http.begin(tlsClient, SERVER_BASE + endpoint);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("ngrok-skip-browser-warning", "true");

    httpCode = http.POST(body);
    responseBody = http.getString();
    http.end();
    return httpCode > 0;
}

bool getJson(const String& endpoint, JsonDocument& doc) {
    if (WiFi.status() != WL_CONNECTED) return false;

    HTTPClient http;
    http.setReuse(true);
    http.begin(tlsClient, SERVER_BASE + endpoint);
    http.addHeader("ngrok-skip-browser-warning", "true");

    int code = http.GET();
    if (code != 200) {
        http.end();
        return false;
    }

    String payload = http.getString();
    http.end();
    return deserializeJson(doc, payload) == DeserializationError::Ok;
}

bool postEmptyJson(const String& endpoint) {
    String responseBody;
    int code = 0;
    if (!postJson(endpoint, "{}", responseBody, code)) return false;
    return code >= 200 && code < 300;
}

void fetchPin() {
    StaticJsonDocument<128> doc;
    if (getJson("/api/get_pin/" + CART_LABEL, doc)) {
        currentPin = doc["pin"] | "WAIT";
    }
}

void syncHardwareState() {
    StaticJsonDocument<256> doc;
    if (!getJson("/api/hardware_state/" + CART_LABEL, doc)) return;

    bool newPendingPlacement = doc["pending_placement"] | false;
    bool newPendingRemoval = doc["pending_removal"] | false;
    float newExpectedWeight = doc["expected_weight_change"] | 0.0f;
    String newStatus = doc["status"] | "idle";
    bool newCheckoutPending = doc["checkout_requested"] | false;
    float newTotalCost = doc["total_cost"] | 0.0f;

    bool placementStarted = newPendingPlacement && !pendingPlacement;
    bool removalStarted = newPendingRemoval && !pendingRemoval;

    pendingPlacement = newPendingPlacement;
    pendingRemoval = newPendingRemoval;
    expectedWeight = newExpectedWeight;
    cartStatus = newStatus;
    checkoutPending = newCheckoutPending;
    totalCost = newTotalCost;

    if (placementStarted || removalStarted) {
        baselineWeight = currentWeight;
        resetVerificationTracking();
        resetIdleSpikeTracking();
        securityAlertRaised = false;
    }
}

void confirmPlacement() {
    if (!postEmptyJson("/api/confirm_placement/" + CART_LABEL)) return;

    beep(100);
    delay(50);
    beep(100);
    lcdPrint("Item added", "Weight OK");
    pendingPlacement = false;
    expectedWeight = 0.0f;
    baselineWeight = currentWeight;
    placementStableStart = 0;
    placementMismatchStart = 0;
    emptyCartStart = 0;
    delay(800);
}

void confirmRemoval() {
    if (!postEmptyJson("/api/confirm_removal/" + CART_LABEL)) return;

    pendingRemoval = false;
    expectedWeight = 0.0f;
    baselineWeight = currentWeight;
    removalStableStart = 0;
    removalMismatchStart = 0;
    emptyCartStart = 0;
}

void triggerSecurityAlert() {
    lcdPrint("Security alert", "Unknown item");
    beep(5000);
    postEmptyJson("/api/report_alert/" + CART_LABEL);
}

void triggerRemovalAlert() {
    lcdPrint("Security alert", "Item removed");
    beep(5000);
    postEmptyJson("/api/report_alert/" + CART_LABEL);
}

void handleLocalScan(const String& barcode) {
    StaticJsonDocument<192> req;
    req["barcode"] = barcode;
    req["cart_label"] = CART_LABEL;

    String body;
    serializeJson(req, body);

    String responseBody;
    int code = 0;
    if (!postJson("/scan", body, responseBody, code)) {
        lcdPrint("Scan failed", "Server offline");
        return;
    }

    StaticJsonDocument<256> res;
    if (deserializeJson(res, responseBody) != DeserializationError::Ok) {
        lcdPrint("Scan failed", "Try again");
        return;
    }

    const char* status = res["status"] | "error";
    if (code == 200 && String(status) == "success") {
        expectedWeight = res["weight"] | 0.0f;
        baselineWeight = currentWeight;
        pendingPlacement = true;
        pendingRemoval = false;
        securityAlertRaised = false;
        emptyCartStart = 0;
        lcdPrint(res["name"] | "Item", "Place in cart");
        return;
    }

    String message = res["message"] | "Scan error";
    lcdPrint("Scan failed", message.substring(0, 16));
}

void readWeight() {
    if (!scale.is_ready()) return;

    float stableWeight = scale.get_units(WEIGHT_SAMPLE_COUNT);
    unsigned long now = millis();

    // Ignore brief micro-spikes from vibration/electrical noise, but keep
    // the full reading path for real placement/removal workflows.
    if (fabsf(stableWeight) <= MICRO_SPIKE_BAND) {
        stableWeight = 0.0f;
    }

    bool bypassSpikeFilter = pendingPlacement || pendingRemoval || checkoutPending;
    if (!bypassSpikeFilter) {
        float jump = fabsf(stableWeight - currentWeight);
        if (jump >= IDLE_SPIKE_JUMP_THRESHOLD) {
            bool sameSpikeWindow = idleSpikeStart != 0
                && fabsf(stableWeight - idleSpikeCandidate) <= (IDLE_SPIKE_JUMP_THRESHOLD * 0.5f);

            if (!sameSpikeWindow) {
                idleSpikeCandidate = stableWeight;
                idleSpikeStart = now;
                return;
            }

            if (now - idleSpikeStart < IDLE_SPIKE_CONFIRM_MS) {
                return;
            }
        }
    }

    resetIdleSpikeTracking();

    currentWeight = stableWeight;
}

void evaluatePendingVerification(bool isPlacement) {
    unsigned long& stableStart = isPlacement ? placementStableStart : removalStableStart;
    unsigned long& mismatchStart = isPlacement ? placementMismatchStart : removalMismatchStart;

    float delta = currentWeight - baselineWeight;
    float expectedAbs = normalizedExpectedWeight(expectedWeight);
    if (expectedAbs <= 0.0f) {
        stableStart = 0;
        mismatchStart = 0;
        return;
    }

    float toleranceAbs = expectedToleranceWeight(expectedAbs);
    float minBound = isPlacement ? (expectedAbs - toleranceAbs) : -(expectedAbs + toleranceAbs);
    float maxBound = isPlacement ? (expectedAbs + toleranceAbs) : -(expectedAbs - toleranceAbs);
    bool inExpectedDirection = isPlacement ? (delta > 0.0f) : (delta < 0.0f);
    bool inRange = inExpectedDirection && delta >= minBound && delta <= maxBound;
    bool clearlyOutOfRange = inExpectedDirection
        && (delta < (minBound - RANGE_EXIT_GRACE) || delta > (maxBound + RANGE_EXIT_GRACE));

    if (inRange) {
        mismatchStart = 0;
        if (stableStart == 0) {
            stableStart = millis();
        }

        if (millis() - stableStart >= EXPECTED_WEIGHT_HOLD_MS) {
            if (isPlacement) {
                confirmPlacement();
            } else {
                confirmRemoval();
            }
        }
        return;
    }

    stableStart = 0;
    if (fabsf(delta) <= IDLE_BASELINE_DRIFT_BAND) {
        mismatchStart = 0;
        return;
    }

    if (!securityAlertRaised && mismatchHeldLongEnough(mismatchStart, clearlyOutOfRange)) {
        if (isPlacement) {
            triggerSecurityAlert();
        } else {
            triggerRemovalAlert();
        }
        securityAlertRaised = true;
        mismatchStart = 0;
    }
}

void handleNegativeAutoTare() {
    if (pendingPlacement || pendingRemoval) {
        negativeWeightStart = 0;
        return;
    }

    if (currentWeight <= NEGATIVE_TARE_THRESHOLD) {
        if (negativeWeightStart == 0) {
            negativeWeightStart = millis();
        }

        if (millis() - negativeWeightStart >= NEGATIVE_TARE_HOLD_MS) {
            tareScale("negative drift");
        }
        return;
    }

    negativeWeightStart = 0;
}

void handleEmptyCartAutoTare() {
    if (pendingPlacement || pendingRemoval) {
        emptyCartStart = 0;
        return;
    }

    if (fabsf(currentWeight) <= EMPTY_CART_BAND) {
        if (emptyCartStart == 0) {
            emptyCartStart = millis();
        }

        if (millis() - emptyCartStart >= EMPTY_AUTO_TARE_MS) {
            tareScale("empty cart timeout");
        }
        return;
    }

    emptyCartStart = 0;
}

void evaluateWeightSecurity() {
    float delta = currentWeight - baselineWeight;

    if (pendingPlacement) {
        evaluatePendingVerification(true);
        return;
    }

    if (pendingRemoval) {
        evaluatePendingVerification(false);
        return;
    }

    if (checkoutPending) {
        if (fabsf(currentWeight) <= EMPTY_CART_BAND) {
            securityAlertRaised = false;
        }
        return;
    }

    // Let tiny baseline drift settle without raising a security warning.
    if (!securityAlertRaised && fabsf(delta) <= IDLE_BASELINE_DRIFT_BAND) {
        baselineWeight = currentWeight;
        unverifiedRiseStart = 0;
        return;
    }

    if (delta > UNVERIFIED_RISE_THRESHOLD && !securityAlertRaised) {
        if (unverifiedRiseStart == 0) {
            unverifiedRiseStart = millis();
        }

        if (millis() - unverifiedRiseStart >= SECURITY_RISE_HOLD_MS) {
            triggerSecurityAlert();
            securityAlertRaised = true;
            baselineWeight = currentWeight;
            unverifiedRiseStart = 0;
        }
    } else if (delta < -UNVERIFIED_DROP_THRESHOLD && !securityAlertRaised) {
        if (unverifiedRiseStart == 0) {
            unverifiedRiseStart = millis();
        }

        if (millis() - unverifiedRiseStart >= SECURITY_RISE_HOLD_MS) {
            triggerRemovalAlert();
            securityAlertRaised = true;
            baselineWeight = currentWeight;
            unverifiedRiseStart = 0;
        }
    } else {
        unverifiedRiseStart = 0;
    }

    if (fabsf(currentWeight) <= EMPTY_CART_BAND) {
        securityAlertRaised = false;
    }
}

void renderStatus() {
    if (WiFi.status() != WL_CONNECTED) {
        lcdPrint("WiFi lost", "Reconnecting");
        return;
    }

    if (pendingPlacement) {
        lcdPrint("Weight " + String(currentWeight, 1) + "g", "Place item now");
        return;
    }

    if (pendingRemoval) {
        lcdPrint("Weight " + String(currentWeight, 1) + "g", "Remove item");
        return;
    }

    if (checkoutPending) {
        lcdPrint("Head to cashier", "Total Rs " + String(totalCost, 0));
        return;
    }

    if (millis() - lastPinFetch >= PIN_FETCH_INTERVAL_MS) {
        fetchPin();
        lastPinFetch = millis();
    }

    if (cartStatus == "idle") {
        lcdPrint("Cart " + CART_LABEL + " ready", "PIN " + currentPin);
        return;
    }

    lcdPrint("Weight " + String(currentWeight, 1) + "g", "PIN " + currentPin);
}

void setup() {
    Serial.begin(115200);
    Serial2.begin(9600);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
    buzzerReady = ledcAttachChannel(BUZZER_PIN, BUZZER_FREQUENCY, BUZZER_RESOLUTION, BUZZER_CHANNEL);
    if (buzzerReady) {
        ledcWriteTone(BUZZER_PIN, 0);
    }

    lcd.init();
    lcd.backlight();
    lcdPrint("Smart Cart " + CART_LABEL, "Starting up");

    tlsClient.setInsecure();

    scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
    scale.set_scale(calibrationFactor);
    tareScale("startup");

    beep(100);
    connectWiFi();
    fetchPin();
    lastPinFetch = millis();
    lcdPrint("WiFi connected", "Weight in grams");
    delay(1000);
}

void loop() {
    ensureWiFi();
    readWeight();
    handleNegativeAutoTare();
    handleEmptyCartAutoTare();

    if (Serial2.available()) {
        String barcode = Serial2.readStringUntil('\n');
        barcode.trim();
        if (barcode.length() > 0) {
            beep(150);
            handleLocalScan(barcode);
        }
    }

    evaluateWeightSecurity();

    if (millis() - lastUpdate >= UPDATE_INTERVAL_MS) {
        lastUpdate = millis();
        syncHardwareState();
        renderStatus();
    }
}
