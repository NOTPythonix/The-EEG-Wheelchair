ALLEY WHEELCHAIR — WASD / MCP4725 / 3.3 V + 5.0 V PROFILES
================================================================

WHAT IS INCLUDED
----------------
- ALLEY_Wheelchair_Dashboard.html: technical wheelchair dashboard, 7-inch
  armrest screen schematic, browser WASD, direction pad, voltage-mode selector,
  EEG roadmap panel, and collapsible debug console.
- ALLEY_Wheelchair_Jetson_Bridge.py: Jetson HTTP/WebSocket server, terminal
  keyboard input, watchdog, and integrated dual-MCP4725 output code.
- mcp4725-bench-set.py: separate attended bench helper that sets 2.5 V on
  both DACs only after a confirmation prompt. Keep the wheelchair controller
  harness physically disconnected when using this helper.
- install-wheelchair-boot-service.sh: installs the dashboard/bridge service.

WASD VOLTAGE MAP (MCP1 / MCP2)
------------------------------
                         3.3 V MODE       5.0 V MODE
Forward                  3.3 / 1.0 V      5.0 / 1.0 V
Backward                 1.0 / 3.3 V      1.0 / 5.0 V
Left                     3.3 / 3.3 V      5.0 / 5.0 V
Right                    1.0 / 1.0 V      1.0 / 1.0 V
Key released / STOP      2.5 / 2.5 V      2.5 / 2.5 V

The 3.3 V option is an OUTPUT CEILING profile. It caps requests above 3.3 V.
The 5.0 V option allows requests up to 5.0 V. It does not physically change
how the DAC boards are powered. The actual board VDD is set by wiring and is
provided to the bridge with --dac-vdd. The bridge never requests more than
min(selected profile, configured DAC VDD).

HOW TO USE THE DASHBOARD
------------------------
1. Start the Jetson bridge.
2. Open the dashboard URL printed by the bridge on a device on the same
   trusted private LAN.
3. Connect to the Jetson if it is not already connected.
4. Select 3.3 V mode or 5.0 V mode in the DAC voltage-mode selector.
5. Hold W/A/S/D or the arrow keys; releasing the key sends STOP (2.5/2.5 V).
   The on-screen direction buttons also send a command while held and STOP
   when released.
6. The Jetson acknowledges commands in the dashboard debug console.

JETSON TERMINAL INPUT NOTE
--------------------------
A normal text terminal does not produce a key-up event. Terminal WASD uses
keyboard auto-repeat and a watchdog, so it returns to STOP about 0.85 seconds
after repeat input ceases. Browser keyup is immediate. For consistent held-key
control, use the dashboard's browser keyboard or on-screen controls.

INSTALL THE DASHBOARD/BRIDGE AT BOOT
-----------------------------------
Extract this ZIP on the Jetson, enter the extracted directory, then run:

  chmod +x install-wheelchair-boot-service.sh
  ./install-wheelchair-boot-service.sh

The installer starts the web dashboard and communication bridge at boot.
It deliberately leaves physical DAC output DISARMED. This allows the UI and
profile selector to work without automatically energizing the wheelchair.

The installer uses these defaults in its systemd unit:
  I2C bus:       not accessed while DAC is disarmed
  MCP1 address:  0x60
  MCP2 address:  0x61
  DAC VDD:       5.0 V (change the service ExecStart if your measured board
                 supply is different)
  Profile:       3.3 V default; changeable from the connected dashboard

RUNNING THE BRIDGE WITH DAC OUTPUT ENABLED (ATTENDED ONLY)
---------------------------------------------------------
Do not run a second bridge while the boot service is still listening on ports
8080 and 8765. Stop the service first:

  sudo systemctl stop alley-wheelchair-bridge.service
  cd /opt/alley-wheelchair
  python3 ALLEY_Wheelchair_Jetson_Bridge.py --enable-dac \
    --i2c-bus 1 --mcp1-address 0x60 --mcp2-address 0x61 \
    --dac-vdd 5.0 --voltage-mode 3.3

The program requires an interactive confirmation before it opens I2C and
writes the initial 2.5/2.5 V request. This prompt is not proof that 2.5 V is
neutral: verify it independently with a multimeter at the wheelchair
controller's joystick signal inputs before connecting the harness. Have a
separate physical power disconnect available. Keep the chair secured on a
stable, load-rated stand and keep hands/clothing clear of moving wheels.

To use the boot dashboard after the attended test, stop the foreground
bridge with Ctrl+C and start the service again:

  sudo systemctl start alley-wheelchair-bridge.service

HARDWARE ELECTRICAL CHECKS — IMPORTANT
--------------------------------------
- Confirm the MCP4725 board VDD with a meter. --dac-vdd must match the real
  supply. A software mode cannot raise the board's physical supply voltage.
- Jetson I2C GPIO is 3.3 V logic and is not 5 V tolerant. Do NOT allow 5 V
  pull-ups from a 5 V-powered DAC breakout to reach Jetson SDA/SCL. Check the
  board schematic; use a suitable bidirectional I2C level shifter or safe
  3.3 V pull-up arrangement as appropriate.
- Confirm bus number and I2C addresses on the actual Jetson before testing.
  Defaults 1, 0x60, and 0x61 are examples and must be verified.
- Confirm the real joystick neutral voltage. The software release command is
  2.5 V / 2.5 V because that is the requested setpoint, not because it has
  been electrically validated as neutral.
- The watchdog and STOP voltage are not safety-rated. Keep a hardware
  emergency disconnect; never rely only on software to stop a wheelchair.

USEFUL SERVICE COMMANDS
-----------------------
  sudo systemctl status alley-wheelchair-bridge.service
  sudo journalctl -u alley-wheelchair-bridge.service -f
  sudo systemctl restart alley-wheelchair-bridge.service
  sudo systemctl disable --now alley-wheelchair-bridge.service

END
