#!/usr/bin/env python3.12
"""
Jetson Nano: NeuroPawn EEG → MCP4725 DAC Controller
---------------------------------------------------
Forward  → 3.3V
Backward → 0.3V
Left     → 3.3V (second DAC)
Right    → 0.3V (second DAC)

Only one DAC is connected now. Enable the second later.
"""

import time
import serial
import board
import busio
import adafruit_mcp4725

# -----------------------------
# CONFIGURATION
# -----------------------------
EEG_PORT = "/dev/ttyUSB0"   # Change if needed
EEG_BAUD = 115200           # NeuroPawn default

VREF = 3.3
DAC_ADDR_1 = 0x60           # First MCP4725
DAC_ADDR_2 = 0x61           # Second MCP4725 (future)
ENABLE_SECOND_DAC = False   # Flip to True when second DAC is connected

# -----------------------------
# INIT I2C + DACs (Jetson Nano pins)
# -----------------------------
i2c = busio.I2C(board.SCL, board.SDA)

dac_forward_back = adafruit_mcp4725.MCP4725(i2c, address=DAC_ADDR_1)
dac_left_right = adafruit_mcp4725.MCP4725(i2c, address=DAC_ADDR_2) if ENABLE_SECOND_DAC else None

print("EEG→DAC controller running on Jetson Nano.")
print(f"Second DAC enabled: {ENABLE_SECOND_DAC}")

# -----------------------------
# DAC HELPER
# -----------------------------
def set_voltage(dac, voltage):
    voltage = max(0.0, min(VREF, voltage))
    dac.normalized = voltage / VREF
    print(f"DAC {hex(dac.address)} → {voltage:.2f}V")

# -----------------------------
# EEG INTENT CLASSIFIER (placeholder)
# Replace with your real classifier later
# -----------------------------
def classify_intent(eeg):
    """
    eeg = [ch1, ch2, ch3, ch4, ch5, ch6, ch7, ch8]
    Replace this logic with your real EEG classifier.
    """
    ch1, ch2, ch3, ch4, ch5, ch6, ch7, ch8 = eeg

    # Example placeholder logic:
    if ch1 > 500: return "forward"
    if ch2 > 500: return "backward"
    if ch3 > 500: return "left"
    if ch4 > 500: return "right"

    return "none"

# -----------------------------
# HANDLE INTENT → DAC OUTPUT
# -----------------------------
def handle_intent(intent):
    if intent == "forward":
        set_voltage(dac_forward_back, 3.3)

    elif intent == "backward":
        set_voltage(dac_forward_back, 0.3)

    elif intent == "left":
        if ENABLE_SECOND_DAC and dac_left_right:
            set_voltage(dac_left_right, 3.3)
        else:
            print("Left ignored (second DAC disabled).")

    elif intent == "right":
        if ENABLE_SECOND_DAC and dac_left_right:
            set_voltage(dac_left_right, 0.3)
        else:
            print("Right ignored (second DAC disabled).")

# -----------------------------
# MAIN LOOP
# -----------------------------
def main():
    print("Connecting to EEG headset...")
    ser = serial.Serial(EEG_PORT, EEG_BAUD, timeout=1)
    print("Connected. Reading EEG...")

    while True:
        try:
            line = ser.readline().decode().strip()
            if not line:
                continue

            eeg_values = list(map(float, line.split(",")))

            intent = classify_intent(eeg_values)
            handle_intent(intent)

        except KeyboardInterrupt:
            print("Stopping.")
            break

        except Exception as e:
            print("Error:", e)
            time.sleep(0.1)

if __name__ == "__main__":
    main()
