import asyncio
import json
import threading
import serial
import numpy as np
from flask import Flask, request
from smbus2 import SMBus
import websockets

# ---------------- MCP4725 ----------------
DAC1 = 0x60
DAC2 = 0x61
VREF = 3.3
RES = 4095
bus = SMBus(1)

def set_voltage(addr, volts):
    volts = max(0.0, min(VREF, float(volts)))
    digital = int((volts / VREF) * RES)
    high = digital >> 4
    low = (digital & 0xF) << 4
    bus.write_i2c_block_data(addr, high, [low])

# ---------------- Flask ----------------
app = Flask(__name__)

@app.route("/set_voltage", methods=["POST"])
def set_voltages():
    data = request.json
    set_voltage(DAC1, data["mcp1"])
    set_voltage(DAC2, data["mcp2"])
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=80)

# ---------------- Serial ----------------
SERIAL_PORT = "/dev/ttyUSB0"
BAUD = 115200
ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)

def parse_eeg_line(line):
    """
    TODO: I will fill this in once you tell me the exact format.
    Must return: (samples_array, sample_rate)
    """
    return None, None

# ---------------- SSVEP detection ----------------
TARGETS = {1:10, 2:12, 3:15, 4:20}

def peak_in_band(freqs, spec, f_center, width=1.0):
    mask = (freqs >= f_center-width) & (freqs <= f_center+width)
    if not np.any(mask):
        return f_center
    band_freqs = freqs[mask]
    band_spec = spec[mask]
    return float(band_freqs[np.argmax(band_spec)])

async def eeg_stream(websocket, path):
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        samples, sample_rate = parse_eeg_line(line)
        if samples is None:
            await asyncio.sleep(0.01)
            continue

        freqs = np.fft.rfftfreq(len(samples), 1.0/sample_rate)
        spec = np.abs(np.fft.rfft(samples))

        box_freqs = {box: peak_in_band(freqs, spec, f) for box, f in TARGETS.items()}
        power = float(np.mean(spec) / (np.max(spec)+1e-9))

        await websocket.send(json.dumps({
            "box_freqs": box_freqs,
            "power": power,
            "battery": 85,
            "signal": "Good"
        }))

        await asyncio.sleep(0.1)

async def run_ws():
    async with websockets.serve(eeg_stream, "0.0.0.0", 8765, ping_interval=None):
        await asyncio.Future()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_ws())
