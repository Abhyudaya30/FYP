#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include "HX711.h"

LiquidCrystal_I2C lcd(0x27, 16, 2);

const int LOADCELL_DOUT_PIN = 20;
const int LOADCELL_SCK_PIN = 21;
HX711 scale;

float calibration_factor = 420.0;
float lastStableWeight = 0;
unsigned long lastTareTime = 0;
bool platformEmpty = true;

void setup() {
    Wire.begin(8, 9);
    lcd.init();
    lcd.backlight();
    
    scale.begin(LOADCELL_DOUT_PIN, 
                LOADCELL_SCK_PIN);
    scale.set_scale(calibration_factor);
    
    // warm up period
    lcd.setCursor(0, 0);
    lcd.print("Warming up...   ");
    lcd.setCursor(0, 1);
    lcd.print("Please wait...  ");
    delay(10000); // 10 second warmup
    
    scale.tare();
    lastTareTime = millis();
    
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Ready!          ");
}

void loop() {
    if (scale.is_ready()) {
        float weight = scale.get_units(20);
        
        // deadband filter
        // ignore changes under 5g
        if (abs(weight - lastStableWeight) < 5.0) {
            weight = lastStableWeight;
        } else {
            lastStableWeight = weight;
        }
        
        // zero out small ghost values
        if (weight < 10 && weight > -10) {
            weight = 0;
            platformEmpty = true;
        } else {
            platformEmpty = false;
        }
        
        // auto retare every 60 seconds
        // ONLY when platform is empty
        if (platformEmpty && 
            millis() - lastTareTime > 60000) {
            
            lcd.clear();
            lcd.setCursor(0, 0);
            lcd.print("Re-zeroing...   ");
            scale.tare();
            lastTareTime = millis();
            lastStableWeight = 0;
            delay(1000);
        }
        
        // display weight
        lcd.setCursor(0, 0);
        lcd.print("Weight:         ");
        lcd.setCursor(8, 0);
        lcd.print(weight, 1);
        lcd.print(" g  ");
        
        // show status on line 2
        lcd.setCursor(0, 1);
        if (platformEmpty) {
            lcd.print("Platform Empty  ");
        } else {
            lcd.print("Item Detected   ");
        }
    } else {
        lcd.setCursor(0, 0);
        lcd.print("HX711 Error!    ");
    }
    
    delay(200);
}
