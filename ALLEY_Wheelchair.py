#!/usr/bin/env python
"""
ALLEY dual MCP4725 controller and local web dashboard.

Terminal logging proves which commands reach this Python bridge:
- WASD keys, STOP, ARM/DISARM
- Manual MCP1/MCP2 voltage setpoints
The log confirms commands received and whether they were applied or blocked.
It does not measure the physical DAC OUT voltage.

MCP1 = X axis; MCP2 = Y axis.
Bench-test prototype only; not a safety-rated wheelchair controller.
"""
import argparse
import json
import logging
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from smbus2 import SMBus
except ImportError:
    try:
        from smbus import SMBus
    except ImportError:
        SMBus = None


# User's specified mapping: MCP1=X, MCP2=Y.
VOLTAGES = {
    "w": (1.00, 1.70),  # Forward
    "s": (3.00, 1.50),  # Backward
    "a": (2.10, 1.00),  # Left
    "d": (2.10, 3.00),  # Right
    "stop": (2.50, 2.50),
}
ACTIONS = {
    "w": "FORWARD",
    "s": "BACKWARD",
    "a": "LEFT",
    "d": "RIGHT",
    "stop": "STOP",
}
MAX_VOLTAGE = 3.30
WATCHDOG_SECONDS = 0.60
HEARTBEAT_SECONDS = 0.15


def log_line(message):
    """Print each bridge event immediately in the Jetson terminal."""
    print(message, flush=True)


def parse_voltage(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be between 0.00 and 3.30 V.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number from 0.00 to 3.30 V.")
    if not math.isfinite(number) or not 0 <= number <= MAX_VOLTAGE:
        raise ValueError(f"{name} must be between 0.00 and 3.30 V.")
    return round(number, 2)


class DualMCP4725:
    def __init__(self, bus_x, address_x, bus_y, address_y, vdd):
        if SMBus is None:
            raise RuntimeError(
                "SMBus module not found. Install python3-smbus or smbus2."
            )
        self.bus_x = SMBus(bus_x)
        try:
            self.bus_y = SMBus(bus_y)
        except Exception:
            self.bus_x.close()
            raise
        self.address_x = address_x
        self.address_y = address_y
        self.vdd = vdd
        self.lock = threading.Lock()

    @staticmethod
    def write_voltage(bus, address, voltage, vdd):
        voltage = max(0.0, min(MAX_VOLTAGE, float(voltage)))
        code = max(0, min(4095, round(voltage / vdd * 4095)))
        # Volatile DAC register write; does not write EEPROM.
        bus.write_i2c_block_data(
            address, 0x40, [(code >> 4) & 0xFF, (code & 0x0F) << 4]
        )

    def set(self, x_voltage, y_voltage):
        with self.lock:
            self.write_voltage(self.bus_x, self.address_x, x_voltage, self.vdd)
            self.write_voltage(self.bus_y, self.address_y, y_voltage, self.vdd)

    def close(self):
        try:
            self.set(*VOLTAGES["stop"])
        finally:
            self.bus_x.close()
            self.bus_y.close()


class Controller:
    def __init__(self, args):
        self.lock = threading.RLock()
        self.dacs = None
        if args.enable_dac:
            self.dacs = DualMCP4725(
                args.bus_x, args.addr_x, args.bus_y, args.addr_y, args.vdd
            )
        self.armed = False
        self.mode = "STOP"
        self.target = VOLTAGES["stop"]
        self.last_heartbeat = time.monotonic()
        if self.dacs:
            self.dacs.set(*self.target)

    def heartbeat(self):
        with self.lock:
            self.last_heartbeat = time.monotonic()

    def set_key(self, key):
        key = str(key).lower().strip()
        if key not in VOLTAGES:
            raise ValueError("Unknown key. Use w, a, s, d, or stop.")

        with self.lock:
            action = ACTIONS[key]
            requested = VOLTAGES[key]

            if key != "stop" and (not self.armed or not self.dacs):
                reason = "DAC DISABLED" if not self.dacs else "DISARMED"
                log_line(
                    f"[BRIDGE CMD] key={key!r} action={action} "
                    f"requested MCP1={requested[0]:.2f}V "
                    f"MCP2={requested[1]:.2f}V -> BLOCKED ({reason}); "
                    f"outputs remain MCP1={self.target[0]:.2f}V "
                    f"MCP2={self.target[1]:.2f}V"
                )
                return

            self.mode = "STOP" if key == "stop" else "WASD"
            self.target = requested
            self.last_heartbeat = time.monotonic()

            if self.dacs and self.armed:
                self.dacs.set(*requested)
                result = "APPLIED TO DACs"
            else:
                result = "RECEIVED ONLY (hardware writes disabled)"

            log_line(
                f"[BRIDGE CMD] key={key!r} action={action} "
                f"-> MCP1={requested[0]:.2f}V MCP2={requested[1]:.2f}V "
                f"result={result}"
            )

    def set_manual(self, raw_x, raw_y):
        # Keep the values as received visible in the terminal, then validate.
        x_voltage = parse_voltage(raw_x, "MCP1 voltage")
        y_voltage = parse_voltage(raw_y, "MCP2 voltage")

        with self.lock:
            if not self.armed or not self.dacs:
                reason = "DAC DISABLED" if not self.dacs else "DISARMED"
                log_line(
                    f"[BRIDGE CMD] MANUAL requested MCP1={x_voltage:.2f}V "
                    f"MCP2={y_voltage:.2f}V -> BLOCKED ({reason})"
                )
                raise RuntimeError(
                    "Manual setpoints were received but not applied. "
                    "Enable DAC hardware and click Arm DAC output first."
                )

            self.mode = "MANUAL"
            self.target = (x_voltage, y_voltage)
            self.last_heartbeat = time.monotonic()
            self.dacs.set(x_voltage, y_voltage)
            log_line(
                f"[BRIDGE CMD] MANUAL -> MCP1={x_voltage:.2f}V "
                f"MCP2={y_voltage:.2f}V result=APPLIED TO DACs"
            )

    def set_armed(self, enabled):
        with self.lock:
            if enabled and not self.dacs:
                log_line("[BRIDGE CMD] ARM requested -> BLOCKED (DAC hardware disabled)")
                raise RuntimeError(
                    "DAC hardware writes are disabled. Restart with --enable-dac."
                )

            self.armed = bool(enabled and self.dacs)
            self.mode = "STOP"
            self.target = VOLTAGES["stop"]
            self.last_heartbeat = time.monotonic()

            if self.dacs:
                self.dacs.set(*self.target)

            state = "ARMED" if self.armed else "DISARMED"
            log_line(
                f"[BRIDGE CMD] {state} -> neutral command "
                f"MCP1={self.target[0]:.2f}V MCP2={self.target[1]:.2f}V"
            )

    def watchdog_loop(self):
        while True:
            time.sleep(0.05)
            with self.lock:
                expired = (
                    self.armed and self.dacs and
                    time.monotonic() - self.last_heartbeat > WATCHDOG_SECONDS
                )
                if expired:
                    self.armed = False
                    self.mode = "STOP"
                    self.target = VOLTAGES["stop"]
                    self.dacs.set(*self.target)
                    log_line(
                        "[BRIDGE WATCHDOG] Browser heartbeat timed out -> "
                        "DISARMED; commanded MCP1=2.50V MCP2=2.50V"
                    )

    def status(self):
        with self.lock:
            return {
                "armed": self.armed,
                "hardware_enabled": bool(self.dacs),
                "mode": self.mode,
                "target": list(self.target),
            }

    def close(self):
        with self.lock:
            self.armed = False
            self.mode = "STOP"
            self.target = VOLTAGES["stop"]
            if self.dacs:
                self.dacs.set(*self.target)
        if self.dacs:
            self.dacs.close()


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALLEY Wheelchair Control</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#08111f;color:#edf5ff;font:16px system-ui}
main{max-width:1050px;margin:auto;padding:26px 16px}h1{font-size:32px;margin:0}
h1 span{color:#56d8e8}.muted,.sub{color:#9aabc2}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px}
.panel{background:#111f32;border:1px solid #2b4059;border-radius:18px;padding:20px}
.pad{display:grid;grid-template-columns:repeat(3,78px);gap:9px;justify-content:center;margin:20px}
button{cursor:pointer;color:#fff;font:inherit;border:1px solid #42627c;background:#20364f;border-radius:13px}
.key{height:68px;font-size:24px;font-weight:bold;touch-action:none}
.stop{background:#612b3b}.key:active{background:#176879}
.action{padding:11px 14px;margin:5px 6px 5px 0}.enable{background:#14543f}.disable{background:#542334}
.apply{background:#14516a;border-color:#2f9ab2}
.value{display:inline-block;background:#0a1728;border:1px solid #2b4059;border-radius:12px;padding:13px;margin:5px;min-width:132px}
.value b{display:block;font-size:24px}.label{font-size:12px;color:#9aabc2;text-transform:uppercase}
.manual-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
input[type=number]{width:100%;padding:12px;margin:7px 0 14px;border-radius:10px;border:1px solid #42627c;background:#091626;color:#fff;font-size:18px}
.warn{background:#332b1c;color:#f4dba8;padding:12px;border-left:3px solid #e8b45a;font-size:13px;line-height:1.45}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #2b4059}
th{color:#9aabc2}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#ff687d;margin-right:8px}
.on{background:#67e3a1}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<h1>ALLEY <span>Wheelchair</span></h1>
<div class="sub">Jetson Orin Nano · Dual MCP4725 · Bridge command monitor</div>
<div class="grid">
<section class="panel">
<h2>WASD movement</h2>
<p><i id="dot" class="dot"></i><span id="status">Connecting…</span></p>
<div class="pad">
<i></i><button class="key" data-k="w">W</button><i></i>
<button class="key" data-k="a">A</button><button class="key stop" data-k="stop">STOP</button><button class="key" data-k="d">D</button>
<i></i><button class="key" data-k="s">S</button><i></i></div>
<button id="arm" class="action enable">Arm DAC output</button>
<button id="disarm" class="action disable">Disarm</button>
<p class="muted">Hold WASD, arrows, or a direction button. Releasing sends STOP.</p>
<div class="warn">BENCH TEST ONLY. Do not connect this prototype to a person-occupied wheelchair. Use an independent hardwired emergency stop. Terminal logs prove commands reached this Python bridge; they do not prove physical DAC output voltage.</div>
</section>
<section class="panel">
<h2>Manual voltage commands</h2>
<p class="muted">Enter independent MCP voltages from 0.00 to 3.30 V.</p>
<div class="manual-grid">
<label><span class="label">MCP1 · X axis</span><input id="manualX" type="number" min="0" max="3.3" step="0.01" value="2.50"></label>
<label><span class="label">MCP2 · Y axis</span><input id="manualY" type="number" min="0" max="3.3" step="0.01" value="2.50"></label>
</div>
<button id="applyManual" class="action apply">Send manual command</button>
<button id="manualStop" class="action disable">STOP / Neutral</button>
<p class="muted">Manual values remain active while the page sends keep-alive heartbeats. Closing the page or losing the connection causes STOP and disarm.</p>
</section>
<section class="panel">
<h2>Command accepted by bridge</h2>
<div class="value"><div class="label">MCP1 command</div><b id="tx">2.50 V</b></div>
<div class="value"><div class="label">MCP2 command</div><b id="ty">2.50 V</b></div>
<p><b>Mode: </b><span id="mode">STOP</span></p>
<p class="muted">For every command, watch the Jetson terminal for a line beginning <code>[BRIDGE RX]</code> followed by <code>[BRIDGE CMD]</code>.</p>
</section>
<section class="panel">
<h2>Voltage map</h2>
<table>
<tr><th>Key</th><th>Action</th><th>MCP1</th><th>MCP2</th></tr>
<tr><td>W</td><td>Forward</td><td>1.00 V</td><td>1.70 V</td></tr>
<tr><td>S</td><td>Backward</td><td>3.00 V</td><td>1.50 V</td></tr>
<tr><td>A</td><td>Left</td><td>2.10 V</td><td>1.00 V</td></tr>
<tr><td>D</td><td>Right</td><td>2.10 V</td><td>3.00 V</td></tr>
<tr><td>STOP</td><td>Neutral</td><td>2.50 V</td><td>2.50 V</td></tr>
</table>
<h2 style="margin-top:18px">Future EEG bridge</h2>
<p class="muted">Put EED.py beside this Python file when you are ready to integrate EEG. This version does not execute EEG commands.</p>
</section></div></main>
<script>
const token="__TOKEN__",headers={"Content-Type":"application/json","X-ALLEY-Token":token};
let armed=false,heartbeatTimer=null,keyRepeatTimer=null;
async function post(path,payload){
 const response=await fetch(path,{method:"POST",headers,body:JSON.stringify(payload)});
 const data=await response.json();
 if(!response.ok)throw new Error(data.error||"Request failed");
 return data;
}
async function sendKey(key){
 try{await post("/api/key",{key});}catch(error){document.getElementById("status").textContent=error.message;}
}
async function heartbeat(){
 if(!armed)return;
 try{await post("/api/heartbeat",{});}catch(error){}
}
function startHeartbeat(){
 if(heartbeatTimer)clearInterval(heartbeatTimer);
 heartbeatTimer=setInterval(heartbeat,150);
}
function clearKeyTimer(){
 if(keyRepeatTimer){clearInterval(keyRepeatTimer);keyRepeatTimer=null;}
 document.querySelectorAll(".key").forEach(button=>button.classList.remove("active"));
}
function stopMovement(){
 clearKeyTimer();
 sendKey("stop");
}
function beginKey(key,button){
 clearKeyTimer();
 // Send one exact command. Heartbeats keep the bridge session alive without
 // flooding the terminal with duplicate WASD command lines.
 sendKey(key);
 if(key!=="stop"&&button&&button.classList)button.classList.add("active");
}
document.querySelectorAll(".key").forEach(button=>{
 button.addEventListener("pointerdown",event=>{event.preventDefault();beginKey(button.dataset.k,button);});
 ["pointerup","pointerleave","pointercancel","lostpointercapture"].forEach(name=>button.addEventListener(name,stopMovement));
});
document.getElementById("arm").onclick=async()=>{
 try{await post("/api/arm",{armed:true});armed=true;startHeartbeat();}
 catch(error){alert(error.message);}
};
document.getElementById("disarm").onclick=async()=>{
 stopMovement();
 try{await post("/api/arm",{armed:false});}catch(error){alert(error.message);}
 armed=false;if(heartbeatTimer){clearInterval(heartbeatTimer);heartbeatTimer=null;}
};
document.getElementById("applyManual").onclick=async()=>{
 const x=document.getElementById("manualX").value,y=document.getElementById("manualY").value;
 if(x===""||y===""){alert("Enter both MCP voltages.");return;}
 const xv=Number(x),yv=Number(y);
 if(!Number.isFinite(xv)||!Number.isFinite(yv)||xv<0||xv>3.3||yv<0||yv>3.3){
  alert("Both voltages must be in the 0.00–3.30 V range.");return;
 }
 try{await post("/api/manual",{x:xv,y:yv});}
 catch(error){alert(error.message);}
};
document.getElementById("manualStop").onclick=()=>stopMovement();

const keyMap={"ArrowUp":"w","ArrowDown":"s","ArrowLeft":"a","ArrowRight":"d"," ":"stop"};
document.addEventListener("keydown",event=>{
 const key=keyMap[event.key]||event.key.toLowerCase();
 if(["w","a","s","d","stop"].includes(key)){
  event.preventDefault();if(!event.repeat)beginKey(key,document.querySelector('[data-k="'+key+'"]'));
 }
});
document.addEventListener("keyup",event=>{
 if(["w","a","s","d"," ","ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(event.key)){
  event.preventDefault();stopMovement();
 }
});
window.addEventListener("blur",stopMovement);
document.addEventListener("visibilitychange",()=>{if(document.hidden)stopMovement();});

async function refresh(){
 try{
  const response=await fetch("/api/status",{headers});
  if(!response.ok)throw new Error("Disconnected");
  const data=await response.json();armed=data.armed;
  document.getElementById("dot").classList.toggle("on",data.hardware_enabled);
  document.getElementById("status").textContent=data.armed?"DAC armed":data.hardware_enabled?"Connected · disarmed":"Connected · hardware disabled";
  document.getElementById("mode").textContent=data.mode;
  document.getElementById("tx").textContent=data.target[0].toFixed(2)+" V";
  document.getElementById("ty").textContent=data.target[1].toFixed(2)+" V";
  if(armed&&!heartbeatTimer)startHeartbeat();
  if(!armed&&heartbeatTimer){clearInterval(heartbeatTimer);heartbeatTimer=null;}
 }catch(error){document.getElementById("status").textContent="Disconnected";}
}
setInterval(refresh,500);refresh();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--enable-dac", action="store_true",
                        help="enable hardware writes; bench testing only")
    parser.add_argument("--bus-x", type=int, default=7)
    parser.add_argument("--addr-x", type=lambda value: int(value, 0), default=0x60)
    parser.add_argument("--bus-y", type=int, default=1)
    parser.add_argument("--addr-y", type=lambda value: int(value, 0), default=0x60)
    parser.add_argument("--vdd", type=float, default=3.3)
    args = parser.parse_args()

    if not 0 < args.vdd <= 3.3:
        parser.error("--vdd must be greater than 0 and no more than 3.3 V.")

    controller = Controller(args)
    token = secrets.token_urlsafe(32)
    threading.Thread(target=controller.watchdog_loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.info(fmt, *args)

        def authorized(self):
            return secrets.compare_digest(
                self.headers.get("X-ALLEY-Token", ""), token
            )

        def send_json(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                page = HTML.replace("__TOKEN__", token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/status":
                if not self.authorized():
                    return self.send_json(401, {"error": "Unauthorized"})
                self.send_json(200, controller.status())
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.authorized():
                log_line(f"[BRIDGE RX] unauthorized POST from {self.client_address[0]}")
                return self.send_json(401, {"error": "Unauthorized"})

            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) or b"{}"
                payload = json.loads(raw.decode("utf-8"))

                # Print the exact JSON message received from the dashboard.
                # Heartbeats are intentionally not printed every 150 ms.
                if path != "/api/heartbeat":
                    log_line(
                        f"[BRIDGE RX] from={self.client_address[0]} "
                        f"endpoint={path} payload={json.dumps(payload, separators=(',', ':'))}"
                    )

                if path == "/api/heartbeat":
                    controller.heartbeat()
                elif path == "/api/key":
                    controller.set_key(payload.get("key", "stop"))
                elif path == "/api/manual":
                    controller.set_manual(payload.get("x"), payload.get("y"))
                elif path == "/api/arm":
                    controller.set_armed(payload.get("armed") is True)
                else:
                    return self.send_error(404)

                self.send_json(200, controller.status())
            except Exception as error:
                log_line(f"[BRIDGE ERROR] endpoint={path} error={error}")
                self.send_json(400, {"error": str(error)})

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 68, flush=True)
    print("ALLEY Wheelchair command bridge started", flush=True)
    print(f"Dashboard: http://<JETSON-IP>:{args.port}/", flush=True)
    print(f"MCP1 X: I2C bus {args.bus_x}, address 0x{args.addr_x:02X}", flush=True)
    print(f"MCP2 Y: I2C bus {args.bus_y}, address 0x{args.addr_y:02X}", flush=True)
    print(f"DAC hardware writes: {'ENABLED' if controller.dacs else 'DISABLED'}", flush=True)
    print("Terminal will show [BRIDGE RX] exact JSON and [BRIDGE CMD] interpretation.", flush=True)
    print("No API key is required for local dashboard-to-Python bridge commands.", flush=True)
    print("Private session token:", token, flush=True)
    print("=" * 68, flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping command bridge...", flush=True)
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
