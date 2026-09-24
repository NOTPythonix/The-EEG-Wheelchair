import serial
from pynput import keyboard

# Open Teensy serial port
teensy = serial.Serial('/dev/ttyACM0', 115200)

print("Press W A S D to send A B C D to Teensy")

def on_press(key):
    try:
        if key.char == 'w':
            teensy.write(b'A')
            print("Sent: A")

        elif key.char == 'a':
            teensy.write(b'B')
            print("Sent: B")

        elif key.char == 's':
            teensy.write(b'C')
            print("Sent: C")

        elif key.char == 'd':
            teensy.write(b'D')
            print("Sent: D")

    except AttributeError:
        pass

with keyboard.Listener(on_press=on_press) as listener:
    listener.join()
