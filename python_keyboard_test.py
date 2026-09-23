#!/usr/bin/env python3.12

import time
import sys
import termios
import tty
from smbus2 import SMBus

# MCP4725 settings
I2C_BUS = 7
DAC_ADDR = 0x60
VREF = 3.3

bus = SMBus(I2C_BUS)

def set_voltage(voltage):
    # Convert voltage to 12-bit DAC value
    dac_value = int((voltage / VREF) * 4095)
    dac_value = max(0, min(4095, dac_value))  # clamp

    # Split into two bytes for MCP4725 fast mode
    high = (dac_value >> 8) & 0x0F
    low = dac_value & 0xFF

    bus.write_i2c_block_data(DAC_ADDR, high, [low])

def get_key():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        key = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return key

print("MCP4725 Voltage Control (W/S keys)")
print("W = 3.3V")
print("S = 0.4V")
print("Default = 2.5V")
print("Press Q to quit.\n")

current_voltage = 2.5
set_voltage(current_voltage)
print(f"Starting at {current_voltage} V")

while True:
    key = get_key().lower()

    if key == "q":
        print("Exiting.")
        break

    if key == "w":
        current_voltage = 3.3
    elif key == "s":
        current_voltage = 0.4
    else:
        current_voltage = 2.5

    set_voltage(current_voltage)
    print(f"Voltage set to {current_voltage:.3f} V")
    time.sleep(0.01)
