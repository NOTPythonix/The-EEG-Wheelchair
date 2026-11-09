#!/usr/bin/env python3
"""Dual MCP4725 WASD dashboard for Jetson Orin Nano. Bench-test prototype only."""
import argparse, json, logging, os, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from smbus2 import SMBus
except ImportError:
    try:
        from smbus import SMBus
    except ImportError:
        SMBus = None

# Latest project voltage table: MCP1=X, MCP2=Y. Output range is clamped to 0-3.3 V.
MAP = {"w": (1.00, 1.70), "s": (3.00, 1.50),
       "a": (2.10, 1.00), "d": (2.10, 3.00), "stop": (2.50, 2.50)}
MAX_V = 3.30
WATCHDOG_S = 0.35

class DACs:
    def __init__(self, bx, ax, by, ay, vdd):
        if SMBus is None:
            raise RuntimeError("Install python3-smbus or smbus2 first.")
        self.xbus, self.ybus = SMBus(bx), SMBus(by)
        self.ax, self.ay, self.vdd = ax, ay, vdd
        self.lock = threading.Lock()

    @staticmethod
    def write(bus, addr, volts, vdd):
        volts = max(0.0, min(MAX_V, float(volts)))
        code = max(0, min(4095, round(volts / vdd * 4095)))
        # MCP4725 volatile DAC register write; does not write EEPROM.
        bus.write_i2c_block_data(addr, 0x40, [(code >> 4) & 255, (code & 15) << 4])

    def set(self, x, y):
        with self.lock:
            self.write(self.xbus, self.ax, x, self.vdd)
            self.write(self.ybus, self.ay, y, self.vdd)

    def close(self):
        try:
            self.set(*MAP["stop"])
        finally:
            self.xbus.close()
            self.ybus.close()

class State:
    def __init__(self, args):
        self.dacs = DACs(args.bus_x, args.addr_x, args.bus_y, args.addr_y, args.vdd) if args.enable_dac else None
        self.armed, self.key = False, "stop"
        self.last = time.monotonic()
        self.lock = threading.RLock()
        self.eed_status = "EED.py not found (WASD mode)"
        self._detect_eed()
        if self.dacs:
            self.dacs.set(*MAP["stop"])

    def _detect_eed(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EED.py")
        if os.path.isfile(path):
            self.eed_status = "EED.py found; EEG command execution is not enabled in this prototype"

    def command(self, key):
        if key not in MAP: key = "stop"
        with self.lock:
            self.key, self.last = key, time.monotonic()
            if self.armed and self.dacs: self.dacs.set(*MAP[key])

    def arm(self, value):
        with self.lock:
            self.armed = bool(value and self.dacs)
            self.key, self.last = "stop", time.monotonic()
            if self.dacs: self.dacs.set(*MAP["stop"])

    def watchdog(self):
        while True:
            time.sleep(.05)
            with self.lock:
                if self.armed and self.dacs and time.monotonic()-self.last > WATCHDOG_S:
                    self.key = "stop"
                    self.dacs.set(*MAP["stop"])

    def status(self):
        with self.lock:
            return {"armed":self.armed, "hardware_enabled":bool(self.dacs),
                    "key":self.key, "volts":MAP[self.key], "eed":self.eed_status}

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALLEY Wheelchair Control</title><style>
*{box-sizing:border-box}body{margin:0;background:#08111f;color:#edf5ff;font:16px system-ui}
main{max-width:960px;margin:auto;padding:26px 16px}h1{font-size:32px;margin:0}h1 span{color:#56d8e8}
.sub,.muted{color:#9aabc2}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px}
.panel{background:#111f32;border:1px solid #2b4059;border-radius:18px;padding:20px}
.pad{display:grid;grid-template-columns:repeat(3,78px);gap:9px;justify-content:center;margin:20px}
button{cursor:pointer;color:white;font:inherit;border:1px solid #42627c;background:#20364f;border-radius:13px}
.key{height:68px;font-size:24px;font-weight:bold;touch-action:none}.stop{background:#612b3b}.key:active{background:#176879}
.action{padding:11px 14px;margin-right:8px}.enable{background:#14543f}.disable{background:#542334}
.value{display:inline-block;background:#0a1728;border:1px solid #2b4059;border-radius:12px;padding:14px;margin:5px}
.value b{display:block;font-size:26px}.warn{background:#332b1c;color:#f4dba8;padding:12px;border-left:3px solid #e8b45a;font-size:13px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #2b4059}th{color:#9aabc2}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#ff687d;margin-right:8px}.on{background:#67e3a1}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body><main><h1>ALLEY <span>Wheelchair</span></h1><div class="sub">Jetson Orin Nano · Dual MCP4725 · local dashboard</div>
<div class="grid"><section class="panel"><h2>Movement</h2><p><i id="dot" class="dot"></i><span id="status">Connecting…</span></p>
<div class="pad"><i></i><button class="key" data-k="w">W</button><i></i>
<button class="key" data-k="a">A</button><button class="key stop" data-k="stop">STOP</button><button class="key" data-k="d">D</button>
<i></i><button class="key" data-k="s">S</button><i></i></div>
<button id="arm" class="action enable">Arm DAC output</button><button id="disarm" class="action disable">Disarm</button>
<p class="muted">Hold WASD, arrow keys, or a direction button. Releasing sends STOP. Space sends STOP.</p>
<div class="warn">BENCH TEST ONLY. Do not connect to a person-occupied wheelchair. Use an independent hardwired emergency stop and validate all failure modes with wheels raised and nobody aboard.</div></section>
<section class="panel"><h2>DAC outputs</h2><div class="value">MCP1 · X<b id="x">2.50 V</b></div><div class="value">MCP2 · Y<b id="y">2.50 V</b></div>
<h2>Voltage table</h2><table><tr><th>Key</th><th>Movement</th><th>X</th><th>Y</th></tr>
<tr><td>W</td><td>Forward</td><td>1.00</td><td>1.70</td></tr><tr><td>S</td><td>Backward</td><td>3.00</td><td>1.50</td></tr>
<tr><td>A</td><td>Left</td><td>2.10</td><td>1.00</td></tr><tr><td>D</td><td>Right</td><td>2.10</td><td>3.00</td></tr><tr><td>STOP</td><td>Neutral</td><td>2.50</td><td>2.50</td></tr></table>
<p class="muted">Clamped range: 0.00–3.30 V.</p></section>
<section class="panel"><h2>Future EEG bridge</h2><b>EED.py</b><p id="eed" class="muted">Checking…</p>
<p class="muted">Put EED.py beside ALLEY_Wheelchair.py. Planned interface: <code>get_command()</code> returns "w", "a", "s", "d", or "stop". This version only detects the file; it does not run EEG commands. EEG needs signal-quality checks and a separate dead-man safety design.</p></section>
<section class="panel"><h2>Connection / safety</h2><p class="muted">Open the printed address from a device on the same trusted Wi-Fi network. Control requests require a per-run access token.</p><p>Command timeout: 350 ms → neutral.</p><p class="muted">A software watchdog is not a safety-rated emergency stop.</p></section></div></main>
<script>
const token="__TOKEN__",H={"Content-Type":"application/json","X-Token":token};let armed=false,repeat=null;
async function post(url,obj){let r=await fetch(url,{method:"POST",headers:H,body:JSON.stringify(obj)});let j=await r.json();if(!r.ok)throw Error(j.error||"request failed");return j}
function stop(){if(repeat){clearInterval(repeat);repeat=null}send("stop");document.querySelectorAll(".key").forEach(x=>x.classList.remove("active"))}
async function send(k){try{await post("/api/key",{key:k})}catch(e){document.getElementById("status").textContent=e.message}}
function hold(k,b){stop();if(k==="stop"||!armed)return; b.classList.add("active");send(k);repeat=setInterval(()=>send(k),100)}
document.querySelectorAll(".key").forEach(b=>{b.onpointerdown=e=>{e.preventDefault();hold(b.dataset.k,b)};["pointerup","pointerleave","pointercancel","lostpointercapture"].forEach(ev=>b.addEventListener(ev,stop))});
document.getElementById("arm").onclick=async()=>{try{await post("/api/arm",{armed:true});armed=true}catch(e){alert(e.message)}};
document.getElementById("disarm").onclick=async()=>{stop();await post("/api/arm",{armed:false});armed=false};
const keys={"ArrowUp":"w","ArrowDown":"s","ArrowLeft":"a","ArrowRight":"d"," ":"stop"};
document.onkeydown=e=>{let k=keys[e.key]||e.key.toLowerCase();if(["w","a","s","d","stop"].includes(k)){e.preventDefault();if(!e.repeat)hold(k,document.querySelector('[data-k="'+k+'"]')||document.body)}};
document.onkeyup=e=>{if(["w","a","s","d"," ","ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(e.key)){e.preventDefault();stop()}};
window.onblur=stop;document.addEventListener("visibilitychange",()=>{if(document.hidden)stop()});
async function refresh(){try{let r=await fetch("/api/status",{headers:H});if(!r.ok)throw Error();let s=await r.json();armed=s.armed;document.getElementById("dot").classList.toggle("on",s.hardware_enabled);document.getElementById("status").textContent=s.armed?"DAC armed":s.hardware_enabled?"Connected · disarmed":"Connected · hardware disabled";document.getElementById("x").textContent=s.volts[0].toFixed(2)+" V";document.getElementById("y").textContent=s.volts[1].toFixed(2)+" V";document.getElementById("eed").textContent=s.eed}catch(e){document.getElementById("status").textContent="Disconnected"}}setInterval(refresh,500);refresh();
</script></body></html>"""

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--host",default="0.0.0.0");p.add_argument("--port",type=int,default=8080)
    p.add_argument("--enable-dac",action="store_true",help="enable hardware writes; bench testing only")
    p.add_argument("--bus-x",type=int,default=7);p.add_argument("--addr-x",type=lambda x:int(x,0),default=0x60)
    p.add_argument("--bus-y",type=int,default=1);p.add_argument("--addr-y",type=lambda x:int(x,0),default=0x60)
    p.add_argument("--vdd",type=float,default=3.3,help="actual measured DAC VDD in volts")
    a=p.parse_args()
    if not 0 < a.vdd <= 3.3: p.error("--vdd must be >0 and <=3.3 V for this configuration")
    state=State(a);token=secrets.token_urlsafe(24)
    threading.Thread(target=state.watchdog,daemon=True).start()
    class H(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args): logging.info(fmt,*args)
        def sendj(self,code,obj):
            b=json.dumps(obj).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
        def auth(self): return secrets.compare_digest(self.headers.get("X-Token",""),token)
        def do_GET(self):
            path=urlparse(self.path).path
            if path=="/":
                b=PAGE.replace("__TOKEN__",token).encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
            elif path=="/api/status" and self.auth(): self.sendj(200,state.status())
            else:self.send_error(401 if path=="/api/status" else 404)
        def do_POST(self):
            if not self.auth():return self.sendj(401,{"error":"Unauthorized"})
            try:
                n=int(self.headers.get("Content-Length","0"));data=json.loads(self.rfile.read(n) or b"{}")
                if self.path=="/api/arm":
                    if data.get("armed") and not state.dacs:return self.sendj(409,{"error":"Start with --enable-dac to enable hardware"})
                    state.arm(data.get("armed") is True)
                elif self.path=="/api/key":state.command(data.get("key","stop"))
                else:return self.send_error(404)
                self.sendj(200,state.status())
            except Exception as e:self.sendj(400,{"error":str(e)})
    server=ThreadingHTTPServer((a.host,a.port),H)
    print("Dashboard: http://<JETSON-IP>:%d/"%a.port)
    print("Hardware writes:", "ENABLED (bench test only)" if state.dacs else "DISABLED")
    print("X bus/address: %s / 0x%02X; Y bus/address: %s / 0x%02X"%(a.bus_x,a.addr_x,a.bus_y,a.addr_y))
    print("Access token (private):",token)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();state.arm(False)
    if state.dacs:state.dacs.close()

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    main()
