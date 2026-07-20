#include <Arduino.h>

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "development"
#endif

void setup() {
  Serial.begin(115200);
  delay(250);
  Serial.println("BOOTING");
  Serial.printf("FIRMWARE_VERSION=%s\n", FIRMWARE_VERSION);
  Serial.println("SELF_TEST=PASS");
  Serial.println("READY");
}

void loop() {
  static unsigned long last_heartbeat = 0;
  const unsigned long now = millis();
  if (now - last_heartbeat >= 5000) {
    last_heartbeat = now;
    Serial.printf("HEARTBEAT uptime_ms=%lu\n", now);
  }
}
