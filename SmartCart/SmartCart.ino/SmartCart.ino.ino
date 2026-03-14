#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

const char* ssid = "GEESE";
const char* password = "biggreencoat";
const char* server = "https://tiesha-gyroidal-drema.ngrok-free.dev"; 
const String cartLabel = "01"; 

#define TRIG_PIN 5
#define ECHO_PIN 18
#define BUZZER_PIN 13
#define SDA_PIN 20   
#define SCL_PIN 21   

LiquidCrystal_I2C lcd(0x27, 16, 2);

float emptyDist = 20.0; 
unsigned long lastApiCheck = 0;
const unsigned long apiInterval = 200; 
String currentStatus = "idle";
bool isScanned = false;
bool isRemoving = false; // Added tracking for removals

void setup() {
  Serial.begin(115200);
  Wire.begin(SDA_PIN, SCL_PIN);
  pinMode(TRIG_PIN, OUTPUT); 
  pinMode(ECHO_PIN, INPUT); 
  pinMode(BUZZER_PIN, OUTPUT);
  lcd.init(); lcd.backlight();
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(200); }
  lcd.clear(); lcd.print("System Ready");
}

float getDistance() {
  digitalWrite(TRIG_PIN, LOW); delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(5); 
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 12000);
  if (duration == 0) return 999; 
  return duration * 0.034 / 2;
}

void updateStatus() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(String(server) + "/api/cart_update/" + cartLabel);
    int httpCode = http.GET();
    if (httpCode == 200) {
      String payload = http.getString();
      int firstPipe = payload.indexOf('|');
      int lastPipe = payload.lastIndexOf('|');
      if (firstPipe != -1) {
        currentStatus = payload.substring(0, firstPipe);
        isScanned = (payload.substring(firstPipe + 1, lastPipe) == "true");
        isRemoving = (payload.substring(lastPipe + 1) == "true");
      }
    }
    http.end();
  }
}

void loop() {
  if (millis() - lastApiCheck > apiInterval) {
    updateStatus();
    lastApiCheck = millis();
  }

  if (currentStatus == "idle") {
    digitalWrite(BUZZER_PIN, LOW);
    lcd.setCursor(0,0); lcd.print("   SMART CART   ");
    lcd.setCursor(0,1); lcd.print(" READY TO SCAN  ");
    return;
  }

  float dist = getDistance();

  // SIDE MOUNT LOGIC (TRIPWIRE)
  if (dist < (emptyDist - 3.0)) { 
    if (isScanned) {
        digitalWrite(BUZZER_PIN, LOW);
        HTTPClient http;
        http.begin(String(server) + "/api/confirm_placement/" + cartLabel);
        http.POST("");
        http.end();
        lcd.clear(); lcd.print("ITEM VERIFIED");
        delay(1000);
    } 
    else if (isRemoving) {
        digitalWrite(BUZZER_PIN, LOW);
        HTTPClient http;
        http.begin(String(server) + "/api/confirm_removal/" + cartLabel);
        http.POST("");
        http.end();
        lcd.clear(); lcd.print("ITEM REMOVED");
        delay(1000);
    }
    else {
        // REPORT ALERT TO WEB APP
        HTTPClient http;
        http.begin(String(server) + "/api/report_alert/" + cartLabel);
        http.POST("");
        http.end();
        
        digitalWrite(BUZZER_PIN, HIGH);
        lcd.clear(); lcd.setCursor(0,0); lcd.print("UNSCANNED ITEM! ");
        lcd.setCursor(0,1); lcd.print("REMOVE PRODUCT  ");
        delay(4000); 
        digitalWrite(BUZZER_PIN, LOW);
        lcd.clear();
    }
  } 
  else {
    digitalWrite(BUZZER_PIN, LOW);
    if (isScanned) {
        lcd.setCursor(0,0); lcd.print("SCAN RECEIVED!  ");
        lcd.setCursor(0,1); lcd.print("PLACE THE ITEM  ");
    } else if (isRemoving) {
        lcd.setCursor(0,0); lcd.print("REMOVE ITEM     ");
        lcd.setCursor(0,1); lcd.print("PLEASE WAIT...  ");
    } else {
        lcd.setCursor(0,0); lcd.print("   SMART CART   ");
        lcd.setCursor(0,1); lcd.print("   ACTIVE...    ");
    }
  }
}