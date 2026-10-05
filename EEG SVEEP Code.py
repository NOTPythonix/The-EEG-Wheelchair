import tkinter as tk
import time
import threading
from smbus2 import SMBus
from pylsl import StreamInlet, resolve_stream

# -----------------------------
# DAC Setup (Two MCP4725)
# -----------------------------
DAC1_ADDR = 0x60
DAC2_ADDR = 0x61
VREF = 3.3
RESOLUTION = 4095
bus = SMBus(1)

def set_voltage(dac_addr, volts):
    volts = max(0, min(volts, VREF))
    digital = int((volts / VREF) * RESOLUTION)
    high = digital >> 4
    low = (digital & 0xF) << 4
    bus.write_i2c_block_data(dac_addr, high, [low])
    print(f"[DAC {hex(dac_addr)}] {volts:.2f} V")

def set_both(v1, v2):
    set_voltage(DAC1_ADDR, v1)
    set_voltage(DAC2_ADDR, v2)
    print(f"[OUTPUT] DAC1={v1:.2f}V  DAC2={v2:.2f}V\n")

# -----------------------------
# SSVEP Frequencies
# -----------------------------
FREQ_FORWARD = 10
FREQ_BACKWARD = 12
FREQ_LEFT = 15
FREQ_RIGHT = 20

MAP_VOLTAGE = {
    FREQ_FORWARD: (3.3, 3.3),
    FREQ_BACKWARD: (0.4, 0.4),
    FREQ_LEFT: (3.3, 0.4),
    FREQ_RIGHT: (0.4, 3.3)
}

# -----------------------------
# GUI Setup
# -----------------------------
root = tk.Tk()
root.title("SSVEP GUI Control")
root.geometry("600x600")
root.configure(bg="black")

boxes = {}

def create_box(name, freq, x, y):
    frame = tk.Frame(root, width=200, height=200, bg="black")
    frame.place(x=x, y=y)
    label = tk.Label(frame, text=name, fg="white", bg="black", font=("Arial", 16))
    label.pack()
    box = tk.Canvas(frame, width=180, height=180, bg="black", highlightthickness=0)
    box.pack()
    boxes[name] = (box, freq)

create_box("FORWARD (10 Hz)", FREQ_FORWARD, 50, 50)
create_box("BACKWARD (12 Hz)", FREQ_BACKWARD, 350, 50)
create_box("LEFT (15 Hz)", FREQ_LEFT, 50, 350)
create_box("RIGHT (20 Hz)", FREQ_RIGHT, 350, 350)

# -----------------------------
# Flicker Animation
# -----------------------------
def flicker(box, freq):
    canvas, _ = boxes[box]
    state = False
    while True:
        color = "white" if state else "black"
        canvas.configure(bg=color)
        state = not state
        time.sleep(1.0 / freq)

def start_flicker_threads():
    for name, (_, freq) in boxes.items():
        threading.Thread(target=flicker, args=(name, freq), daemon=True).start()

# -----------------------------
# EEG Frequency Detection
# -----------------------------
def detect_ssvep():
    print("Searching for EEG stream...")
    streams = resolve_stream('type', 'EEG')
    inlet = StreamInlet(streams[0])
    print("EEG stream connected.\n")

    while True:
        sample, ts = inlet.pull_sample()
        detected_freq = int(sample[0])

        if detected_freq in MAP_VOLTAGE:
            v1, v2 = MAP_VOLTAGE[detected_freq]
            set_both(v1, v2)

            print(f"[COMMAND] Frequency {detected_freq} Hz")

        time.sleep(0.05)

# -----------------------------
# MAIN
# -----------------------------
threading.Thread(target=start_flicker_threads, daemon=True).start()
threading.Thread(target=detect_ssvep, daemon=True).start()

root.mainloop()
