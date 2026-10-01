/*
  EEG to 5-pin joystick replacement for Arduino Nano.
  VRx and VRy are PWM outputs. SW is active-low.
*/
#include <Wire.h>
const bool USE_I2C_EEG = false;
const byte EEG_I2C_ADDR = 0x57;
const byte EEG_ANALOG_PIN = A0;
const byte VRX_PIN = 5;
const byte VRY_PIN = 6;
const byte SW_PIN = 4;
const int JOY_MIN = 0;
const int JOY_CENTER = 127;
const int JOY_MAX = 255;
const int THRESH_FORWARD = 600;
const int THRESH_BACKWARD = 300;
const int THRESH_RIGHT = 700;
const int THRESH_LEFT = 200;
const int THRESH_BLINK = 900;
const byte SMOOTH_SAMPLES = 10;
const byte RAMP_STEP = 4;
const unsigned long LOOP_INTERVAL_MS = 20;
const int DEADZONE = 50;
const bool DEBUG = true;
const unsigned long BAUD = 115200;
int baseline = 512;
int smoothBuf[SMOOTH_SAMPLES];
byte smoothIndex = 0;
int rawEEG = 0;
int smoothEEG = 0;
int vrxTarget = JOY_CENTER;
int vryTarget = JOY_CENTER;
int vrxOut = JOY_CENTER;
int vryOut = JOY_CENTER;
bool buttonReleased = true;
unsigned long lastLoop = 0;
void setup() {
  pinMode(VRX_PIN, OUTPUT);
  pinMode(VRY_PIN, OUTPUT);
  pinMode(SW_PIN, OUTPUT);
  if (USE_I2C_EEG) Wire.begin();
  if (DEBUG) Serial.begin(BAUD);
  centerJoystick();
  for (byte i = 0; i < SMOOTH_SAMPLES; i++) smoothBuf[i] = baseline;
  baseline = readAverageEEG(100, 20);
  if (DEBUG) {
    Serial.print(F("Baseline: "));
    Serial.println(baseline);
  }
}
void loop() {
  if (millis() - lastLoop < LOOP_INTERVAL_MS) return;
  lastLoop = millis();
  rawEEG = readEEG();
  smoothEEG = smooth(rawEEG);
  updateTargets(smoothEEG);
  vrxOut = rampToward(vrxOut, vrxTarget);
  vryOut = rampToward(vryOut, vryTarget);
  analogWrite(VRX_PIN, vrxOut);
  analogWrite(VRY_PIN, vryOut);
  digitalWrite(SW_PIN, buttonReleased ? HIGH : LOW);
  baseline += (smoothEEG - baseline) / 500;
  if (DEBUG) printDebug();
}
int readEEG() {
  if (!USE_I2C_EEG) return analogRead(EEG_ANALOG_PIN);
  Wire.requestFrom(EEG_I2C_ADDR, (byte)2);
  if (Wire.available() < 2) return baseline;
  int value = (Wire.read() << 8) | Wire.read();
  return constrain(value, 0, 1023);
}
int smooth(int value) {
  smoothBuf[smoothIndex] = value;
  smoothIndex = (smoothIndex + 1) % SMOOTH_SAMPLES;
