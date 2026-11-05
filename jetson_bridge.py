"""ALLEY Mobility Jetson command bridge with configurable MCP4725 profiles.

User-configured MCP1/MCP2 movement map:
  forward:  5.0 V / 1.0 V
  backward: 1.0 V / 5.0 V
  left:     5.0 V / 5.0 V
  right:    1.0 V / 1.0 V
  released/STOP: 2.5 V / 2.5 V

Selectable output ceiling:
  3.3 V mode: any 5.0 V setpoint is capped at 3.3 V.
  5.0 V mode: setpoints are capped at 5.0 V.
The actual MCP4725 VDD is a separate hardware value supplied with --dac-vdd;
no selected mode can exceed the actual configured DAC VDD.

IMPORTANT: DAC outputs are disabled unless --enable-dac is supplied and a
human confirms startup. Before connecting to a wheelchair controller, verify
both joystick-neutral inputs, DAC supply, I2C voltage levels, output mapping,
and a separate physical emergency-power disconnect. This is bench/control
integration software, not a safety-rated wheelchair controller.

Browser keyup sends STOP immediately. The Jetson terminal's basic text input
does not report key-release events, so terminal WASD relies on the watchdog
and returns to STOP after the configured timeout once key-repeat input ceases.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hmac
import ipaddress
import json
import os
import select
import signal
import socket
import ssl
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from websockets.asyncio.server import serve
except ImportError:  # Compatibility with older Ubuntu/Jetson websockets packages.
    from websockets import serve  # type: ignore

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore
    tty = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
HTTP_PORT = 8080
WEBSOCKET_PORT = 8765
WATCHDOG_SECONDS = 0.85
ALLOWED_DIRECTIONS = {"forward", "backward", "left", "right", "stop"}
TERMINAL_KEYS = {
    "w": "forward",
    "a": "left",
    "s": "backward",
    "d": "right",
    " ": "stop",
    "x": "stop",
}

# MCP4725 fast-write interface. These are defaults only; verify bus and
# addresses with the boards connected and the wheelchair control harness isolated.
I2C_SLAVE = 0x0703
VOLTAGE_MAP: dict[str, tuple[float, float]] = {
    "forward": (5.0, 1.0),
    "backward": (1.0, 5.0),
    "left": (5.0, 5.0),
    "right": (1.0, 1.0),
    "stop": (2.5, 2.5),  # Key released / neutral request.
}
VOLTAGE_MODES = {"3.3", "5.0"}
current_voltage_mode = "3.3"
configured_dac_vdd = 5.0

clients: set[Any] = set()
BRIDGE_TOKEN = ""
ALLOWED_NETWORKS: list[ipaddress._BaseNetwork] = []
current_direction = "stop"
current_source = "startup"
last_command_at = time.monotonic()
sequence_number = 0
shutdown_event = threading.Event()
dac_pair: "MCP4725Pair | None" = None


def json_message(message_type: str, **fields: Any) -> str:
    return json.dumps({"type": message_type, **fields}, separators=(",", ":"))


class MCP4725Pair:
    """Writes volatile DAC values to two MCP4725s over Linux I2C."""

    def __init__(self, bus_number: int, address1: int, address2: int, vdd: float) -> None:
        if address1 == address2:
            raise ValueError("MCP4725 boards must have different I2C addresses")
        if not (0x03 <= address1 <= 0x77 and 0x03 <= address2 <= 0x77):
            raise ValueError("I2C addresses must be valid 7-bit addresses")
        if vdd <= 0:
            raise ValueError("DAC VDD must be greater than zero")
        self.bus_number = bus_number
        self.address1 = address1
        self.address2 = address2
        self.vdd = vdd
        self.path = f"/dev/i2c-{bus_number}"
        self.fd = os.open(self.path, os.O_RDWR)

    def _write_voltage(self, address: int, volts: float) -> None:
        if volts < 0 or volts > self.vdd:
            raise ValueError(f"Requested {volts:.3f} V is outside DAC VDD range 0..{self.vdd:.3f} V")
        # MCP4725 12-bit fast mode: 0x40 | D11..D8, followed by D7..D0.
        code = max(0, min(4095, round((volts / self.vdd) * 4095)))
        first = 0x40 | ((code >> 8) & 0x0F)
        second = code & 0xFF
        fcntl.ioctl(self.fd, I2C_SLAVE, address)
        written = os.write(self.fd, bytes((first, second)))
        if written != 2:
            raise OSError(f"Short I2C write to MCP4725 0x{address:02X}")

    def write_pair(self, volts1: float, volts2: float) -> None:
        # Both values are validated before either device is updated.
        for value in (volts1, volts2):
            if value < 0 or value > self.vdd:
                raise ValueError(f"Requested voltage outside DAC VDD range 0..{self.vdd:.3f} V")
        self._write_voltage(self.address1, volts1)
        self._write_voltage(self.address2, volts2)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None  # type: ignore[assignment]


def hardware_status() -> str:
    return "enabled" if dac_pair is not None else "disabled"


def effective_voltage_cap() -> float:
    """Selected mode cap, limited again by the measured/configured DAC VDD."""
    return min(float(current_voltage_mode), configured_dac_vdd)


def get_voltage_pair(direction: str) -> tuple[float, float]:
    raw_pair = VOLTAGE_MAP[direction]
    cap = effective_voltage_cap()
    return (min(raw_pair[0], cap), min(raw_pair[1], cap))


def public_voltage_map() -> dict[str, list[float]]:
    return {name: list(get_voltage_pair(name)) for name in VOLTAGE_MAP}


def apply_dac_direction(direction: str) -> None:
    """Apply the current mode's pair only when DAC output is explicitly armed."""
    if dac_pair is None:
        return
    pair = get_voltage_pair(direction)
    try:
        dac_pair.write_pair(*pair)
        print(
            f"[DAC] {direction.upper()} ({current_voltage_mode} V mode) -> "
            f"MCP1={pair[0]:.3f} V, MCP2={pair[1]:.3f} V",
            flush=True,
        )
    except Exception as exc:
        print(f"[DAC ERROR] {exc}; attempting 2.5/2.5 V STOP", flush=True)
        try:
            dac_pair.write_pair(*get_voltage_pair("stop"))
        except Exception as stop_exc:
            print(f"[DAC CRITICAL] STOP write also failed: {stop_exc}", flush=True)
        raise


async def broadcast(message: str) -> None:
    if not clients:
        return
    stale: list[Any] = []
    for client in list(clients):
        try:
            await client.send(message)
        except Exception:
            stale.append(client)
    for client in stale:
        clients.discard(client)


def set_state(direction: str, source: str) -> bool:
    """Update command state; write DAC only when explicitly armed."""
    global current_direction, current_source, last_command_at
    changed = direction != current_direction or source != current_source
    current_direction = direction
    current_source = source
    last_command_at = time.monotonic()
    if changed:
        apply_dac_direction(direction)
    return changed


async def websocket_handler(websocket: Any, path: str | None = None) -> None:
    global current_voltage_mode
    peer = "unknown"
    authenticated = False
    try:
        remote = getattr(websocket, "remote_address", None)
        if remote:
            peer = str(remote[0])
            try:
                peer_ip = ipaddress.ip_address(str(remote[0]).split("%")[0])
            except ValueError:
                await websocket.close(code=1008, reason="Invalid client address")
                return
            in_allowed_lan = peer_ip.is_loopback or any(peer_ip in network for network in ALLOWED_NETWORKS)
            if not in_allowed_lan:
                print(f"[SECURITY] Rejected client outside configured Jetson LAN subnets: {peer}", flush=True)
                await websocket.close(code=1008, reason="Same-LAN clients only")
                return

        # The browser sends the shared, per-install token only after the
        # WebSocket is established. No movement commands are accepted first.
        await websocket.send(json_message("auth_required", method="shared_token"))
        try:
            raw_auth = await asyncio.wait_for(websocket.recv(), timeout=10)
            auth_data = json.loads(raw_auth) if isinstance(raw_auth, str) else {}
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as exc:
            print(f"[SECURITY] Authentication handshake failed from {peer}: {exc.__class__.__name__}", flush=True)
            await websocket.close(code=1008, reason="Authentication required")
            return

        supplied_token = auth_data.get("token", "") if isinstance(auth_data, dict) and auth_data.get("type") == "auth" else ""
        if not isinstance(supplied_token, str) or not hmac.compare_digest(supplied_token, BRIDGE_TOKEN):
            print(f"[SECURITY] Rejected invalid dashboard token from {peer}", flush=True)
            try:
                await websocket.send(json_message("auth_error", message="Invalid local bridge token"))
            finally:
                await websocket.close(code=1008, reason="Invalid token")
            return

        authenticated = True
        clients.add(websocket)
        await websocket.send(json_message("auth_ok", message="Authenticated over TLS"))
        await websocket.send(json_message(
            "hello",
            direction=current_direction,
            source=current_source,
            hardware_output=hardware_status(),
            watchdog_seconds=WATCHDOG_SECONDS,
            terminal_keys=bool(sys.stdin.isatty()),
            message="Jetson bridge connected; DAC output " + hardware_status(),
            voltage_mode=current_voltage_mode,
            voltage_cap=effective_voltage_cap(),
            dac_vdd=configured_dac_vdd,
            voltage_map=public_voltage_map(),
        ))
        print(f"[LINK] Authenticated dashboard connected from {peer}", flush=True)

        async for raw in websocket:
            if not isinstance(raw, str) or len(raw) > 4096:
                await websocket.send(json_message("error", message="Invalid or oversized message"))
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(json_message("error", message="Message was not valid JSON"))
                continue
            if not isinstance(data, dict):
                await websocket.send(json_message("error", message="Message must be a JSON object"))
                continue
            if data.get("type") == "voltage_mode":
                requested_mode = str(data.get("mode", ""))
                if requested_mode not in VOLTAGE_MODES:
                    await websocket.send(json_message("error", message="Voltage mode must be 3.3 or 5.0"))
                    continue
                if current_direction != "stop":
                    await websocket.send(json_message("error", message="Release the movement key and STOP before changing voltage mode"))
                    continue
                current_voltage_mode = requested_mode
                try:
                    apply_dac_direction("stop")
                except Exception as exc:
                    await websocket.send(json_message("error", message=f"Mode changed but STOP write failed: {exc}"))
                    continue
                mode_message = json_message(
                    "mode_state", voltage_mode=current_voltage_mode,
                    voltage_cap=effective_voltage_cap(), dac_vdd=configured_dac_vdd,
                    voltage_map=public_voltage_map(),
                )
                await broadcast(mode_message)
                await websocket.send(json_message(
                    "mode_ack", voltage_mode=current_voltage_mode,
                    voltage_cap=effective_voltage_cap(),
                    hardware_output=hardware_status(),
                ))
                print(f"[MODE] Output ceiling changed to {current_voltage_mode} V (effective cap {effective_voltage_cap():.2f} V)", flush=True)
                continue

            if data.get("type") != "command":
                await websocket.send(json_message("error", message="Unsupported message type"))
                continue
            direction = data.get("direction")
            if direction not in ALLOWED_DIRECTIONS:
                await websocket.send(json_message("error", message="Direction not allowed"))
                continue
            source = str(data.get("source", "browser"))[:40]
            seq = data.get("seq")
            changed = set_state(direction, source)
            if changed:
                print(f"[RX] {source}: {direction.upper()} (sequence={seq})", flush=True)
                await broadcast(json_message(
                    "state", direction=current_direction, source=current_source,
                    origin="browser", hardware_output=hardware_status(),
                ))
            await websocket.send(json_message(
                "ack", seq=seq, direction=direction, source=source,
                received=True, hardware_output=hardware_status(),
            ))
    except Exception as exc:
        print(f"[LINK] Client {peer} disconnected: {exc.__class__.__name__}", flush=True)
    finally:
        clients.discard(websocket)
        if authenticated and current_source.startswith("browser") and current_direction != "stop":
            try:
                set_state("stop", "connection_lost")
            except Exception:
                pass
            await broadcast(json_message(
                "state", direction="stop", source="connection_lost",
                origin="watchdog", hardware_output=hardware_status(),
            ))
            print("[SAFE STOP] Browser link closed; STOP requested", flush=True)


async def watchdog_loop() -> None:
    while not shutdown_event.is_set():
        await asyncio.sleep(0.10)
        if current_direction != "stop" and time.monotonic() - last_command_at > WATCHDOG_SECONDS:
            try:
                set_state("stop", "watchdog_timeout")
            except Exception:
                pass
            print("[SAFE STOP] No fresh command; STOP requested", flush=True)
            await broadcast(json_message(
                "state", direction="stop", source="watchdog_timeout",
                origin="watchdog", hardware_output=hardware_status(),
            ))


async def terminal_input_loop(queue: asyncio.Queue[str]) -> None:
    while not shutdown_event.is_set():
        direction = await queue.get()
        try:
            changed = set_state(direction, "terminal")
        except Exception:
            changed = False
        if changed:
            print(f"[TX] terminal keyboard: {direction.upper()}", flush=True)
            await broadcast(json_message(
                "terminal_command", direction=direction, source="terminal",
                hardware_output=hardware_status(),
            ))


def terminal_keyboard_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
    if not sys.stdin.isatty() or termios is None or tty is None:
        print("[TERM] Terminal keyboard input unavailable (stdin is not a TTY).", flush=True)
        return
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        new_settings = termios.tcgetattr(fd)
        new_settings[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new_settings)
        print("[TERM] Keyboard ready: W/A/S/D; SPACE or X = STOP; Ctrl+C exits.", flush=True)
        while not shutdown_event.is_set():
            ready, _, _ = select.select([fd], [], [], 0.10)
            if not ready:
                continue
            char = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
            if char in TERMINAL_KEYS:
                loop.call_soon_threadsafe(queue.put_nowait, TERMINAL_KEYS[char])
    except (OSError, ValueError) as exc:
        print(f"[TERM] Keyboard reader stopped: {exc}", flush=True)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def arm_dac_if_requested(args: argparse.Namespace) -> None:
    global dac_pair
    if not args.enable_dac:
        print("[DAC] Hardware output is DISABLED (default).", flush=True)
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Refusing to arm DAC output without an interactive terminal")
    print("\n*** PHYSICAL WHEELCHAIR DAC OUTPUT ARMING ***")
    print("Configured release/STOP request is 2.5 V on BOTH axes; this is NOT verified neutral.")
    print(f"Selected mode: {current_voltage_mode} V | effective output cap: {effective_voltage_cap():.2f} V")
    print("Verify the wheelchair joystick neutral voltage before connecting the controller harness.")
    print("Use a stable, load-rated stand, keep wheels clear, and have a physical power disconnect ready.")
    print("Never rely on this software watchdog as the only emergency stop.")
    confirmation = input("Type VERIFIED-2P5V-NEUTRAL-AND-SECURED to continue: ").strip()
    if confirmation != "VERIFIED-2P5V-NEUTRAL-AND-SECURED":
        raise RuntimeError("DAC arming cancelled")
    dac_pair = MCP4725Pair(args.i2c_bus, args.mcp1_address, args.mcp2_address, args.dac_vdd)
    # Apply requested stop only after explicit arming confirmation.
    apply_dac_direction("stop")
    print(f"[DAC] Armed on I2C bus {args.i2c_bus}, addresses 0x{args.mcp1_address:02X}/0x{args.mcp2_address:02X}, VDD={args.dac_vdd:.2f} V", flush=True)


async def run(args: argparse.Namespace) -> None:
    global clients, current_voltage_mode, configured_dac_vdd, BRIDGE_TOKEN, ALLOWED_NETWORKS
    current_voltage_mode = args.voltage_mode
    configured_dac_vdd = args.dac_vdd

    token_path = Path(args.token_file)
    if not token_path.is_file():
        raise RuntimeError(f"Bridge token file not found: {token_path}. Run install-github-pages-jetson.sh first.")
    BRIDGE_TOKEN = token_path.read_text(encoding="utf-8").strip()
    if len(BRIDGE_TOKEN) < 32:
        raise RuntimeError("Bridge token must contain at least 32 characters; regenerate the token file.")
    if not args.allowed_subnet:
        raise RuntimeError("No allowed LAN subnet configured. Re-run the installer or provide --allowed-subnet CIDR.")
    try:
        ALLOWED_NETWORKS = [ipaddress.ip_network(item, strict=False) for item in args.allowed_subnet]
    except ValueError as exc:
        raise RuntimeError(f"Invalid --allowed-subnet value: {exc}") from exc

    cert_path = Path(args.tls_cert)
    key_path = Path(args.tls_key)
    if not cert_path.is_file() or not key_path.is_file():
        raise RuntimeError("TLS certificate/key not found. Run install-github-pages-jetson.sh first.")
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    arm_dac_if_requested(args)
    handler = partial(SimpleHTTPRequestHandler, directory=str(BASE_DIR))
    # Optional local preview is loopback-only. The GitHub Pages site is the
    # intended dashboard and connects to the WSS port below.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), handler)
    httpd.daemon_threads = True
    http_thread = threading.Thread(target=httpd.serve_forever, name="dashboard-http", daemon=True)
    http_thread.start()

    loop = asyncio.get_running_loop()
    keyboard_queue: asyncio.Queue[str] = asyncio.Queue()
    if not args.no_terminal_keys and sys.stdin.isatty():
        keyboard_thread = threading.Thread(
            target=terminal_keyboard_reader,
            args=(loop, keyboard_queue),
            name="terminal-keyboard", daemon=True,
        )
        keyboard_thread.start()
    else:
        print("[TERM] Terminal WASD disabled; use browser controls instead.", flush=True)

    local_host = socket.gethostname().split(".")[0]
    print("\nALLEY MOBILITY — Jetson LAN command bridge", flush=True)
    print(f"GitHub Pages dashboard: publish index.html to your GitHub Pages site", flush=True)
    print(f"Secure WebSocket: wss://{local_host}.local:{args.ws_port}", flush=True)
    print("Allowed LAN subnet(s): " + ", ".join(str(network) for network in ALLOWED_NETWORKS), flush=True)
    print(f"Local preview on Jetson only: http://127.0.0.1:{args.http_port}/index.html", flush=True)
    print(f"Network commands: TOKEN REQUIRED | TLS REQUIRED | MCP4725 output: {hardware_status().upper()}", flush=True)
    print(f"Voltage mode: {current_voltage_mode} V (effective cap {effective_voltage_cap():.2f} V)", flush=True)
    print("Voltage map (MCP1/MCP2): FWD 5/1 V | BACK 1/5 V | LEFT 5/5 V | RIGHT 1/1 V | RELEASE/STOP 2.5/2.5 V", flush=True)
    print("Keep the Jetson and controller on a trusted private Wi-Fi LAN. Do not port-forward this service.\n", flush=True)

    try:
        async with serve(
            websocket_handler, "0.0.0.0", args.ws_port,
            ssl=tls_context, ping_interval=20, ping_timeout=20, max_size=4096,
        ):
            tasks = [asyncio.create_task(watchdog_loop())]
            if not args.no_terminal_keys and sys.stdin.isatty():
                tasks.append(asyncio.create_task(terminal_input_loop(keyboard_queue)))
            await asyncio.gather(*tasks)
    finally:
        shutdown_event.set()
        try:
            if dac_pair is not None:
                set_state("stop", "shutdown")
        except Exception as exc:
            print(f"[DAC CRITICAL] Could not issue shutdown STOP: {exc}", flush=True)
        httpd.shutdown()
        httpd.server_close()
        if dac_pair is not None:
            dac_pair.close()
            print("[DAC] I2C connection closed", flush=True)
        print("\n[STOP] Bridge stopped.", flush=True)


def parse_address(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Address must be decimal or hex, e.g. 0x60") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ALLEY Mobility Jetson command bridge")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT)
    parser.add_argument("--ws-port", type=int, default=WEBSOCKET_PORT)
    parser.add_argument("--no-terminal-keys", action="store_true")
    parser.add_argument("--tls-cert", default="/etc/alley-wheelchair/tls/server.crt", help="TLS certificate for secure WSS connections")
    parser.add_argument("--tls-key", default="/etc/alley-wheelchair/tls/server.key", help="TLS private key for secure WSS connections")
    parser.add_argument("--token-file", default="/etc/alley-wheelchair/bridge.token", help="Private shared-token file required by dashboards")
    parser.add_argument("--allowed-subnet", action="append", default=[], help="Allowed client LAN subnet in CIDR form; may be repeated")
    parser.add_argument("--enable-dac", action="store_true", help="Arm MCP4725 output after interactive confirmation")
    parser.add_argument("--i2c-bus", type=int, default=1, help="I2C bus number (default: 1; verify before use)")
    parser.add_argument("--mcp1-address", type=parse_address, default=0x60, help="MCP1 7-bit I2C address (default: 0x60)")
    parser.add_argument("--mcp2-address", type=parse_address, default=0x61, help="MCP2 7-bit I2C address (default: 0x61)")
    parser.add_argument("--dac-vdd", type=float, default=5.0, help="Measured physical MCP4725 VDD in volts (default: 5.0)")
    parser.add_argument("--voltage-mode", choices=("3.3", "5.0"), default="3.3", help="Selected max output profile (default: 3.3 V)")
    args = parser.parse_args()
    if not (1 <= args.http_port <= 65535 and 1 <= args.ws_port <= 65535):
        parser.error("Ports must be between 1 and 65535")
    if args.http_port == args.ws_port:
        parser.error("HTTP and WebSocket ports must be different")
    if args.i2c_bus < 0:
        parser.error("I2C bus must be nonnegative")
    if args.mcp1_address == args.mcp2_address:
        parser.error("MCP1 and MCP2 addresses must be different")
    if args.dac_vdd < 2.5:
        parser.error("--dac-vdd must be at least 2.5 V to produce the configured 2.5 V release/STOP request")
    return args


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        shutdown_event.set()
        print("\nStopped by user.", flush=True)
    except Exception as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
