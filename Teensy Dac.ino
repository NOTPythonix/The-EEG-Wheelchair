// Teensy 4.1 DAC output on pin A21 (pin 14)

const int DAC_PIN = A21;   // Teensy 4.1 DAC output

void setup() {
  Serial.begin(115200);
}

void loop() {
  if (Serial.available()) {
    char cmd = Serial.read();

    switch (cmd) {
      case 'A':
        analogWrite(DAC_PIN, voltageToDAC(3.0));   // 3.0V
        break;

      case 'B':
        analogWrite(DAC_PIN, voltageToDAC(0.3));   // 0.3V
        break;

      case 'C':
        analogWrite(DAC_PIN, voltageToDAC(3.0));   // 3.0V
        break;

      case 'D':
        analogWrite(DAC_PIN, voltageToDAC(0.3));   // 0.3V
        break;
    }
  }
}

// Convert voltage (0–3.3V) to 12‑bit DAC value
int voltageToDAC(float volts) {
  float dacMax = 4095.0;     // 12‑bit DAC resolution
  float vRef   = 3.3;        // DAC reference voltage
  return (int)((volts / vRef) * dacMax);
}
