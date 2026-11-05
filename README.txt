ALLEY WHEELCHAIR — GITHUB PAGES + JETSON SAME-WI-FI SETUP
==========================================================

WHAT THIS DOES
--------------
- GitHub Pages hosts the static dashboard (index.html).
- The Jetson Orin Nano runs the Python bridge and talks to the two MCP4725
  boards over I2C.
- The browser connects to the Jetson over TLS WebSocket (WSS) on port 8765.
- A per-install access token is required before the bridge accepts commands.
- The bridge accepts private/LAN client IPs only.
- The browser device and Jetson must be on the same trusted Wi-Fi/LAN.

IMPORTANT SECURITY / HARDWARE DEFAULTS
--------------------------------------
- This setup does NOT enable physical DAC output. The bridge starts with MCP4725
  output DISARMED, and this installer does not add --enable-dac to systemd.
- The bridge's STOP request remains the configured 2.5 V / 2.5 V pair. It has
  not been validated as the wheelchair's actual neutral voltage.
- Do not port-forward TCP 8765. Do not expose the bridge to the internet.
- A software watchdog is not a safety-rated emergency stop. Keep an independent
  physical power disconnect and verify the powered-chair controller separately.
- The 3.3 V / 5.0 V selector is an output ceiling, not a way to change the
  physical MCP4725 supply. --dac-vdd must match the measured DAC VDD.
- Jetson I2C pins are 3.3 V logic and are not 5 V tolerant. Protect SDA/SCL
  from 5 V pull-ups; use an appropriate I2C level shifter if required.

PART A — PUBLISH THE DASHBOARD ON GITHUB PAGES
----------------------------------------------
1. In your GitHub repository, upload this file as index.html in the publishing
   folder (usually the repository root).
2. Open repository Settings > Pages.
3. Under Build and deployment, choose Deploy from a branch.
4. Choose branch main (or the branch you use) and folder /(root), then Save.
5. Wait for GitHub Pages to publish the site. Open the HTTPS URL shown there.
6. Keep the site on HTTPS. The dashboard uses WSS to connect to the Jetson.

PART B — INSTALL THE JETSON BRIDGE
---------------------------------
1. Copy these files to one folder on the Jetson:
     ALLEY_Wheelchair_Jetson_Bridge.py
     index.html
     install-github-pages-jetson.sh
2. In a Jetson terminal, go into that folder. Example:
     cd ~/Downloads/alley-wheelchair-github-pages
3. Run:
     chmod +x install-github-pages-jetson.sh
     ./install-github-pages-jetson.sh
4. The installer:
   - installs Python WebSocket support, OpenSSL and Avahi mDNS;
   - creates a local certificate authority and a TLS server certificate;
   - creates a long random bridge access token;
   - installs the service and starts it at boot;
   - keeps MCP4725 output DISARMED.
5. At the end, write down the displayed values:
     Jetson hostname, such as orin.local
     Bridge token (a long random string)
     CA certificate path
     CA SHA-256 fingerprint

The exact hostname depends on the Jetson's Linux hostname. Avahi publishes it
as <hostname>.local. The installer also includes the detected Jetson LAN IPv4
address(es) in the certificate, so you can use a printed IP if mDNS does not
resolve on your client device.

PART C — TRUST THE JETSON CERTIFICATE ON YOUR BROWSER DEVICE
------------------------------------------------------------
The GitHub Pages site is hosted on HTTPS, so its local Jetson connection uses
WSS. The browser must trust the Jetson's local CA certificate.

Copy the public CA certificate from the Jetson to your browser device. You can
use a USB flash drive, or SCP from another computer's terminal (replace the
username/IP):

  scp JETSON_USERNAME@JETSON_LAN_IP:/etc/alley-wheelchair/tls/ca.crt ~/Downloads/alley-wheelchair-ca.crt

On a Chromebook without Linux enabled, use a USB flash drive: copy the
ca.crt file from the Jetson onto the drive, then open the drive in the
Chromebook Files app and import that file from ChromeOS certificate settings.

You can find the Jetson LAN IP with this command on the Jetson:

  hostname -I

Compare the downloaded certificate's fingerprint with the installer output:

  openssl x509 -in ~/Downloads/alley-wheelchair-ca.crt -noout -fingerprint -sha256

Firefox (Linux desktop):
1. Open Firefox Settings.
2. Go to Privacy & Security.
3. Scroll to Certificates and select View Certificates.
4. Open the Authorities tab and choose Import.
5. Select alley-wheelchair-ca.crt.
6. Trust this CA to identify websites, then confirm.
7. Restart/reload the GitHub Pages tab.

Chrome / Chromium / ChromeOS:
- Open the browser or operating-system certificate manager.
- Import alley-wheelchair-ca.crt under Authorities / certificate authorities.
- Trust it for identifying websites, then restart/reload the site.
- The exact menu wording differs by operating-system version.

Only trust this CA on devices you control. The CA certificate is public, but
never copy or share the CA private key at /etc/alley-wheelchair/tls/ca.key.

PART D — CONNECT FROM THE GITHUB PAGES DASHBOARD
------------------------------------------------
1. Open the published GitHub Pages HTTPS URL on a device on the same Wi-Fi.
2. In the Jetson hostname box, type the hostname printed by the installer,
   including .local (example: orin.local).
3. In Private LAN access token, paste the exact random token printed by the
   installer. You can display it again on the Jetson with:

     sudo cat /etc/alley-wheelchair/bridge.token

4. Press Connect.
5. The dashboard should show Jetson connected and the debug console should
   report TLS authentication and bridge status.
6. Use the dashboard's STOP control to request the 2.5 V / 2.5 V configured
   release pair. Verify neutral electrically before connecting to the chair.

If using Chrome/Chromium, the browser may show a Local Network Access permission
prompt because a public website is connecting to a private LAN device. Allow
local-network access for your GitHub Pages site. Chrome documents this prompt
for local-network requests. If you deny it, change the permission in that
site's browser settings and retry.

PART E — FIREWALL: SAME-LAN ONLY
-------------------------------
The bridge itself rejects non-private client IP addresses and requires the
random token. Also limit port 8765 to the local subnet using the Jetson firewall
when UFW is active. Find the subnet with:

  ip -4 route

Then allow only that subnet (replace the example with the LAN subnet shown by
ip -4 route):

  sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp

Do NOT use `sudo ufw allow 8765` for all sources. Do not configure router port
forwarding for this port. Guest Wi-Fi / AP isolation may prevent client-to-client
connections; use the same trusted non-guest LAN.

SERVICE COMMANDS
----------------
  sudo systemctl status alley-wheelchair-bridge.service
  sudo journalctl -u alley-wheelchair-bridge.service -f
  sudo systemctl restart alley-wheelchair-bridge.service
  sudo systemctl disable --now alley-wheelchair-bridge.service

TROUBLESHOOTING
---------------
- Host name does not resolve: make sure avahi-daemon is active and both devices
  are on the same LAN. On Jetson: `systemctl status avahi-daemon`.
- TLS/certificate error: verify that the CA was imported on the client browser,
  the certificate was copied from this Jetson, and the hostname exactly matches
  the installer output (including .local).
- Authentication error: re-copy the current token using the `sudo cat` command.
- Connection timeout: verify the bridge service, same Wi-Fi, firewall rule, and
  that port 8765 is not blocked by guest-network isolation.
- Browser local-network prompt: allow access for the GitHub Pages site.
- Dashboard shows connected but no hardware movement: the bridge is intentionally
  DISARMED at boot; a secure connection does not mean the DACs are active.

VOLTAGE MAPPING (MCP1 / MCP2)
----------------------------
                         3.3 V MODE       5.0 V MODE
Forward                  3.3 / 1.0 V      5.0 / 1.0 V
Backward                 1.0 / 3.3 V      1.0 / 5.0 V
Left                     3.3 / 3.3 V      5.0 / 5.0 V
Right                    1.0 / 1.0 V      1.0 / 1.0 V
Released / STOP          2.5 / 2.5 V      2.5 / 2.5 V

The bridge only writes these voltages if DAC output has been deliberately
armed in a separate attended test. This GitHub Pages installation does not
arm DAC output or verify that any requested voltage is neutral for the chair.
