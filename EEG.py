#!/usr/bin/env python3
"""Future EEG headset input module for ALLEY_Wheelchair.py.

This file is intentionally a safe starter template, not a working EEG decoder.
Put your future headset / ESP32-S3 USB-serial reader and classifier in this file.

Expected future data path (planned):
    Neuropawn / Knight Board headset -> ESP32-S3 USB CDC -> Jetson USB
    Serial target (planned): 115200 baud, 8-N-1
    Frame layout (planned): 0xA0 + 57-byte EEG/IMU payload + 0xC0

When your code recognizes a stable command, call:
    send_command("forward")
    send_command("backward")
    send_command("left")
    send_command("right")
    send_command("stop")

The parent wheelchair controller validates commands and applies its watchdog.
This module does NOT access the MCP4725 DACs directly.
"""
from __future__ import annotations

from collections.abc import Callable

VALID_COMMANDS = {"forward", "backward", "left", "right", "stop"}
_command_callback: Callable[[str], None] | None = None
_running = False


def start(command_callback: Callable[[str], None]) -> None:
    """Prepare the EEG module when ALLEY_Wheelchair.py starts with --enable-eeg.

    Add your headset connection / reader startup here later. Keep the reader in
    its own thread or async task so it does not block the wheelchair web server.
    """
    global _command_callback, _running
    if not callable(command_callback):
        raise TypeError("command_callback must be callable")

    _command_callback = command_callback
    _running = True
    print("[EEG] EEG.py placeholder loaded; no headset reader is configured.")
    print("[EEG] Add your decoder here, then call send_command() for recognized states.")


def send_command(command: str) -> bool:
    """Publish one recognized command to ALLEY_Wheelchair.py.

    Call this only after your decoder has validated the frame and determined a
    sufficiently stable intent. Return False if the module is stopped or the
    command is not one of the five allowed direction strings.
    """
    if not _running or _command_callback is None:
        return False
    if not isinstance(command, str):
        return False

    normalized = command.strip().lower()
    if normalized not in VALID_COMMANDS:
        return False

    _command_callback(normalized)
    return True


def handle_eeg_frame(frame: bytes) -> None:
    """TODO: Validate and decode one complete headset frame here.

    Suggested work for the future implementation:
      1. Check frame start (0xA0), end (0xC0), and expected length.
      2. Check sequence numbers and reject dropped/corrupt frames.
      3. Decode EEG/IMU channels and calculate classifier confidence.
      4. Require a stable command for a short dwell period before publishing.
      5. Call send_command("forward"/"backward"/"left"/"right"/"stop").

    Do not publish movement from a single noisy sample. This function is a
    placeholder and deliberately does nothing until you implement it.
    """
    _ = frame
    return None


def stop() -> None:
    """Stop the future EEG reader and clear the controller callback."""
    global _command_callback, _running
    _running = False
    _command_callback = None
    print("[EEG] EEG module stopped.")


if __name__ == "__main__":
    print("EEG.py is an integration template. Run the parent ALLEY_Wheelchair.py program instead.")
