ALLEY WHEELCHAIR — FUTURE EEG INPUT TEMPLATE
============================================

FILES
-----
ALLEY_Wheelchair.py                 Main wheelchair dashboard / bridge program
EEG.py                              Future EEG headset code goes here
ALLEY_Wheelchair_Jetson_Bridge.py   Same updated bridge under its previous filename

WHAT CHANGED
------------
- The main controller can optionally load EEG.py using --enable-eeg.
- EEG.py is a starter template. It does not connect to a headset or generate
  movement commands until you add the decoder/reader code.
- EEG commands pass through the same controller state path and watchdog as
  other commands. A timeout requests STOP.
- EEG.py does not write to the MCP4725 DACs directly.
- DAC output is still DISABLED by default. --enable-eeg does not arm the DACs.

INSTALL
-------
Copy ALLEY_Wheelchair.py and EEG.py into the same folder on the Jetson.
For example:

  mkdir -p ~/The-EEG-Wheelchair
  cd ~/The-EEG-Wheelchair

Copy both files into that directory, then run the normal dashboard mode:

  python3 ALLEY_Wheelchair.py

To load the EEG extension point (still no headset decoding and no DAC output):

  python3 ALLEY_Wheelchair.py --enable-eeg

The existing bridge setup still requires its token file and --allowed-subnet
configuration, normally supplied by your installed service/launcher. If you
start the script manually, use the same arguments as your current installation.

HOW TO ADD YOUR EEG CODE LATER
-----------------------------
1. Open EEG.py.
2. Add the ESP32-S3 USB serial reader and frame parser in this file.
3. Validate the planned frame structure before using channel data:
     start byte: 0xA0
     payload:   57 bytes (EEG + IMU)
     end byte:  0xC0
     serial:    115200 baud, 8-N-1 (planned)
4. In your classifier, publish only stable, validated movement intents by calling
   send_command("forward"), send_command("backward"), send_command("left"),
   send_command("right"), or send_command("stop").
5. Keep EEG updates flowing while a movement state is held. If fresh commands
   stop arriving, the bridge watchdog requests STOP after 0.85 seconds.

SAFETY
------
- Start with DAC output DISABLED. Verify EEG parsing and command display first.
- Do not use EEG commands to move a person-carrying wheelchair until the entire
  system has independent safety review and controlled bench testing.
- EEG classification is noisy by nature. Add confidence thresholds, stable
  dwell time, neutral/release handling, stale-data detection, and fail-to-STOP.
- Do not rely on this software watchdog as a safety-rated emergency stop.
- Keep a physical emergency power disconnect available.
- The existing STOP request is 2.5 V / 2.5 V and has not been independently
  verified as the chair controller's true neutral voltage.
