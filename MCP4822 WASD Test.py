import sys
import termios
import tty
import board
import busio
import digitalio
import mcp48xx

# --- SPI Setup ---
spi = busio.SPI(board.SCK, board.MOSI)  # MCP4822 uses only MOSI + SCK
cs = digitalio.DigitalInOut(board.CE0)  # Chip Select (Pin 24)

dac = mcp48xx.MCP4822(spi, cs)

# Two channels
chA = dac.channel_a
chB = dac.channel_b

# Gain = 2 gives 0–4.096 V range
chA.gain = 2
chB.gain = 2
chA.active = True
chB.active = True

VREF = 4.096  # MCP4822 gain=2 reference

# Voltage presets
HIGH_V = 3.0
LOW_V  = 0.3

def set_voltage(channel, volts):
    channel.normalized_value = volts / VREF
    print(f"Set {channel} to {volts:.3f} V")

# --- Keyboard reader ---
def get_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

print("W/S control OUTA, A/D control OUTB. Press Q to quit.\n")

while True:
    key = get_key().lower()

    if key == "q":
        print("Exiting.")
        break

    # Channel A (OUTA)
    if key == "w":
        set_voltage(chA, HIGH_V)
    elif key == "s":
        set_voltage(chA, LOW_V)

    # Channel B (OUTB)
    elif key == "a":
        set_voltage(chB, HIGH_V)
    elif key == "d":
        set_voltage(chB, LOW_V)
