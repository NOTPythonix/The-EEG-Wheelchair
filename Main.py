#!/usr/bin/env python3
"""
dac_server_with_ui.py

Flask server that:
- Serves a static HTML UI (index.html) and assets.
- Provides DAC control endpoints: /set_dac, /enable_dac, /set_both, /read_debug, /status.

Run:
  sudo python3 dac_server_with_ui.py

Access UI from another device:
  http://<JETSON_IP>:5000/
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from smbus2 import SMBus, i2c_msg
import serial
import serial.tools.list_ports
import threading
import time
import os

# ---------- Configuration ----------
I2C_BUS = 1
DAC_ADDRS = [0x60, 0x61]
VREF = 3.3
MAX_CODE = 4095
DEFAULT_DAC1_ENABLED = False

# Path to the folder containing your HTML (index.html) and any JS/CSS
# Example: /home/jetson/webui
STATIC_FOLDER = os.path.join(os.path.dirname(__file__), "webui")

# Flask app
app = Flask(__name__, static_folder=STATIC_FOLDER, static_url_path="")
CORS(app)

# ---------- I2C / DAC state ----------
dac_enabled = [True, DEFAULT_DAC1_ENABLED]
i2c_lock = threading.Lock()

try:
    bus = SMBus(I2C_BUS)
except Exception as e:
    bus = None
    print("Warning: Could not open I2C bus:", e)


def voltage_to_code(voltage: float, vref: float = VREF) -> int:
    if voltage < 0:
        voltage = 0.0
    if voltage > vref:
        voltage = vref
    return int(round((voltage / vref) * MAX_CODE))


def write_mcp4725_fast(addr: int, code: int) -> None:
    msb = (code >> 4) & 0xFF
    lsb = (code & 0x0F) << 4
    if bus is None:
        raise RuntimeError("I2C bus not initialized")
    with i2c_lock:
        try:
            bus.write_i2c_block_data(addr, 0x40, [msb, lsb])
        except Exception:
            msg = i2c_msg.write(addr, [msb, lsb])
            bus.i2c_rdwr(msg)


# ---------- Static file serving ----------
# Serve index.html at root
@app.route("/")
def index():
    # If index.html exists in STATIC_FOLDER, serve it
    index_path = os.path.join(STATIC_FOLDER, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(STATIC_FOLDER, "index.html")
    return "<h3>Index file not found. Put index.html in the webui folder.</h3>", 404

# Serve other static files (JS/CSS)
@app.route("/<path:filename>")
def static_files(filename):
    file_path = os.path.join(STATIC_FOLDER, filename)
    if os.path.exists(file_path):
        return send_from_directory(STATIC_FOLDER, filename)
    return "Not found", 404


# ---------- DAC endpoints (same behavior as earlier) ----------
@app.route("/set_dac", methods=["POST"])
def set_dac():
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Missing JSON body"}), 400
    dac = data.get("dac")
    voltage = data.get("voltage")
    if dac not in (0, 1):
        return jsonify({"error": "dac must be 0 or 1"}), 400
    if voltage is None:
        return jsonify({"error": "voltage required"}), 400
    if not dac_enabled[dac]:
        return jsonify({"status": "disabled", "dac": dac}), 200
    try:
        code = voltage_to_code(float(voltage))
        write_mcp4725_fast(DAC_ADDRS[dac], code)
        return jsonify({"status": "ok", "dac": dac, "voltage": float(voltage), "code": code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/enable_dac", methods=["POST"])
def enable_dac():
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Missing JSON body"}), 400
    dac = data.get("dac")
    enabled = data.get("enabled")
    if dac not in (0, 1):
        return jsonify({"error": "dac must be 0 or 1"}), 400
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be boolean"}), 400
    dac_enabled[dac] = enabled
    if not enabled:
        try:
            write_mcp4725_fast(DAC_ADDRS[dac], voltage_to_code(0.0))
        except Exception:
            pass
    return jsonify({"status": "ok", "dac": dac, "enabled": enabled})


@app.route("/set_both", methods=["POST"])
def set_both():
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Missing JSON body"}), 400
    voltage = data.get("voltage")
    force = bool(data.get("force", False))
    if voltage is None:
        return jsonify({"error": "voltage required"}), 400
    code = voltage_to_code(float(voltage))
    results = []
    for idx in (0, 1):
        if not dac_enabled[idx] and not force:
            results.append({"dac": idx, "skipped": True})
            continue
        try:
            write_mcp4725_fast(DAC_ADDRS[idx], code)
            results.append({"dac": idx, "voltage": float(voltage), "code": code})
        except Exception as e:
            results.append({"dac": idx, "error": str(e)})
    return jsonify({"status": "ok", "results": results})


@app.route("/read_debug", methods=["GET"])
def read_debug():
    found = {}
    ports = list(serial.tools.list_ports.comports())
    dev_paths = set([p.device for p in ports])
    for prefix in ("/dev/ttyUSB", "/dev/ttyACM", "/dev/rfcomm", "/dev/ttyS"):
        for i in range(0, 8):
            path = f"{prefix}{i}"
            if os.path.exists(path):
                dev_paths.add(path)
    for dev in sorted(dev_paths):
        try:
            ser = serial.Serial(dev, baudrate=115200, timeout=0.2)
            time.sleep(0.05)
            data = b""
            try:
                data = ser.read(1024)
            finally:
                ser.close()
            if data:
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(data)
                found[dev] = {"raw_len": len(data), "text": text}
            else:
                found[dev] = {"raw_len": 0, "text": ""}
        except Exception as e:
            found[dev] = {"error": str(e)}
    return jsonify({"timestamp": time.time(), "devices": found})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "i2c_bus": I2C_BUS,
        "dac_addresses": DAC_ADDRS,
        "dac_enabled": dac_enabled,
        "vref": VREF
    })


# ---------- Run server ----------
if __name__ == "__main__":
    # Ensure the static folder exists
    if not os.path.isdir(STATIC_FOLDER):
        print("Static folder not found:", STATIC_FOLDER)
        print("Create a folder named 'webui' next to this script and put index.html inside it.")
    # Bind to 0.0.0.0 so the server is reachable by IP on the LAN
    app.run(host="0.0.0.0", port=5000, debug=False)
