import serial
import time
import sys

# -----------------------------
# CONFIGURE YOUR EEG DEVICE PORT
# -----------------------------
# Try: /dev/ttyUSB0, /dev/ttyACM0, /dev/ttyTHS1, /dev/ttyAMA0
EEG_PORT = "/dev/ttyUSB0"
BAUD_RATE = 115200

def open_port():
    try:
        ser = serial.Serial(
            port=EEG_PORT,
            baudrate=BAUD_RATE,
            timeout=0.1,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE
        )
        print(f"[OK] Connected to EEG headset on {EEG_PORT}")
        return ser
    except Exception as e:
        print(f"[ERROR] Cannot open port {EEG_PORT}: {e}")
        sys.exit(1)

def read_eeg_stream(ser):
    print("[INFO] Reading EEG data stream... Press CTRL+C to stop.\n")

    while True:
        try:
            raw = ser.read(512)  # read up to 512 bytes at a time

            if raw:
                try:
                    # Try decoding as UTF‑8 text
                    text = raw.decode("utf-8", errors="ignore").strip()
                    print(f"[TEXT] {text}")
                except:
                    # If not text, show raw bytes
                    print(f"[BIN] {raw.hex()}")

        except KeyboardInterrupt:
            print("\n[STOP] EEG stream stopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            break

def main():
    ser = open_port()
    time.sleep(1)
    read_eeg_stream(ser)
    ser.close()

if __name__ == "__main__":
    main()
