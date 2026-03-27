#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <LiquidCrystal_I2C.h>
#include <Wire.h>
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
const int BUZZER_FREQUENCY = 2400;
const int BUZZER_RESOLUTION = 8;

// --- OBJECTS ---
HX711 scale;
LiquidCrystal_I2C lcd(0x27, 16, 2);
WiFiClientSecure tlsClient;

// --- WEIGHT CONFIG ---
float calibrationFactor = 114.2f;
const float NOISE_BAND = 1.5f;
const float EMPTY_CART_BAND = 5.0f;
const float NEGATIVE_TARE_THRESHOLD = -2.5f;
const float UNVERIFIED_RISE_THRESHOLD = 20.0f;
const float PRODUCT_WEIGHT_TOLERANCE = 0.10f;  // 10%
const float RANGE_EXIT_GRACE = 3.0f;
const float WEIGHT_SMOOTHING_ALPHA = 0.18f;
const int WEIGHT_SAMPLE_COUNT = 8;

// --- TIMINGS ---
const unsigned long UPDATE_INTERVAL_MS = 350;
const unsigned long PIN_FETCH_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_INTERVAL_MS = 2500;
const unsigned long NEGATIVE_TARE_HOLD_MS = 1200;
const unsigned long EMPTY_AUTO_TARE_MS = 15000;
const unsigned long EXPECTED_WEIGHT_HOLD_MS = 900;
const unsigned long SECURITY_RISE_HOLD_MS = 900;

// --- RUNTIME STATE ---
float currentWeight = 0.0f;
float filteredWeight = 0.0f;
float baselineWeight = 0.0f;
float expectedWeight = 0.0f;

String currentPin = "WAIT";
String cartStatus = "idle";

bool pendingPlacement = false;
bool pendingRemoval = false;
bool securityAlertRaised = false;

unsigned long lastUpdate = 0;
unsigned long lastPinFetch = 0;
unsigned long lastWifiRetry = 0;
unsigned long negativeWeightStart = 0;
unsigned long emptyCartStart = 0;
unsigned long placementStableStart = 0;
unsigned long removalStableStart = 0;
unsigned long unverifiedRiseStart = 0;

void beep(int durationMs) {
    ledcWriteTone(BUZZER_PIN, BUZZER_FREQUENCY);
    delay(durationMs);
    ledcWriteTone(BUZZER_PIN, 0);
}

void lcdPrint(const String& line1, const String& line2) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print(line1.substring(0, 16));
    lcd.setCursor(0, 1);
    lcd.print(line2.substring(0, 16));
}

void resetScaleTracking() {
    baselineWeight = 0.0f;
    currentWeight = 0.0f;
    filteredWeight = 0.0f;
    negativeWeightStart = 0;
    emptyCartStart = millis();
    placementStableStart = 0;
    removalStableStart = 0;
    unverifiedRiseStart = 0;
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

    bool placementStarted = newPendingPlacement && !pendingPlacement;
    bool removalStarted = newPendingRemoval && !pendingRemoval;

    pendingPlacement = newPendingPlacement;
    pendingRemoval = newPendingRemoval;
    expectedWeight = newExpectedWeight;
    cartStatus = newStatus;

    if (placementStarted || removalStarted) {
        baselineWeight = currentWeight;
        placementStableStart = 0;
        removalStableStart = 0;
        unverifiedRiseStart = 0;
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
    emptyCartStart = 0;
    delay(800);
}

void confirmRemoval() {
    if (!postEmptyJson("/api/confirm_removal/" + CART_LABEL)) return;

    pendingRemoval = false;
    expectedWeight = 0.0f;
    baselineWeight = currentWeight;
    removalStableStart = 0;
    emptyCartStart = 0;
}

void triggerSecurityAlert() {
    lcdPrint("Security alert", "Unknown item");
    for (int i = 0; i < 3; i++) {
        beep(100);
        delay(50);
    }
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

    float rawWeight = scale.get_units(WEIGHT_SAMPLE_COUNT);

    // Smooth the HX711 output so brief electrical noise and Wi-Fi activity
    // do not cause large weight jumps on the cart UI.
    if (filteredWeight == 0.0f && fabsf(rawWeight) > NOISE_BAND) {
        filteredWeight = rawWeight;
    } else {
        filteredWeight = (WEIGHT_SMOOTHING_ALPHA * rawWeight) +
                         ((1.0f - WEIGHT_SMOOTHING_ALPHA) * filteredWeight);
    }

    if (filteredWeight > -NOISE_BAND && filteredWeight < NOISE_BAND) {
        filteredWeight = 0.0f;
    }

    currentWeight = filteredWeight;
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
        float minBound = expectedWeight * (1.0f - PRODUCT_WEIGHT_TOLERANCE);
        float maxBound = expectedWeight * (1.0f + PRODUCT_WEIGHT_TOLERANCE);
        bool inRange = delta >= minBound && delta <= maxBound;
        bool clearlyOutOfRange = delta < (minBound - RANGE_EXIT_GRACE) || delta > (maxBound + RANGE_EXIT_GRACE);

        if (inRange) {
            if (placementStableStart == 0) {
                placementStableStart = millis();
            }

            if (millis() - placementStableStart >= EXPECTED_WEIGHT_HOLD_MS) {
                confirmPlacement();
            }
        } else if (clearlyOutOfRange) {
            placementStableStart = 0;
        }
        return;
    }

    if (pendingRemoval) {
        float minBound = -expectedWeight * (1.0f + PRODUCT_WEIGHT_TOLERANCE);
        float maxBound = -expectedWeight * (1.0f - PRODUCT_WEIGHT_TOLERANCE);
        bool inRange = delta >= minBound && delta <= maxBound;
        bool clearlyOutOfRange = delta < (minBound - RANGE_EXIT_GRACE) || delta > (maxBound + RANGE_EXIT_GRACE);

        if (inRange) {
            if (removalStableStart == 0) {
                removalStableStart = millis();
            }

            if (millis() - removalStableStart >= EXPECTED_WEIGHT_HOLD_MS) {
                confirmRemoval();
            }
        } else if (clearlyOutOfRange) {
            removalStableStart = 0;
        }
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

    if (cartStatus == "idle") {
        if (millis() - lastPinFetch >= PIN_FETCH_INTERVAL_MS) {
            fetchPin();
            lastPinFetch = millis();
        }
        lcdPrint("Cart " + CART_LABEL + " ready", "PIN " + currentPin);
        return;
    }

    lcdPrint("Weight " + String(currentWeight, 1) + "g", "Scan next item");
}

void setup() {
    Serial.begin(115200);
    Serial2.begin(9600);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    pinMode(BUZZER_PIN, OUTPUT);
    ledcAttach(BUZZER_PIN, BUZZER_FREQUENCY, BUZZER_RESOLUTION);
    ledcWriteTone(BUZZER_PIN, 0);

    lcd.init();
    lcd.backlight();
    lcdPrint("Smart Cart " + CART_LABEL, "Starting up");

    tlsClient.setInsecure();

    scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
    scale.set_scale(calibrationFactor);
    tareScale("startup");

    beep(100);
    connectWiFi();
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
