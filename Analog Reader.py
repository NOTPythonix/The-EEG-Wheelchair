import time

# Pin Definition (Physical Pin 18)

INPUT_PIN = 18

def main():

    # Set the pin numbering mode to physical BOARD numbers

    GPIO.setmode(GPIO.BOARD)

    # Set up the input pin with an internal pull-down resistor 

    # (Note: Orin Nano hardware support for internal pull-ups/downs varies; 

    # external resistors are recommended for reliability).

    GPIO.setup(INPUT_PIN, GPIO.IN)

    print(f"Starting GPIO read on physical pin {INPUT_PIN}...")

    print("Press Ctrl+C to exit.")

    try:

        while True:

            # Read the value of the pin

            value = GPIO.input(INPUT_PIN)

            

            if value == GPIO.HIGH:

                print("Signal Detected: HIGH")

            else:

                print("Signal Detected: LOW")

            # Polling delay

            time.sleep(0.1)

    except KeyboardInterrupt:

        print("\nTerminating script...")

    finally:

        # Release GPIO resources

        GPIO.cleanup()

        print("GPIO Cleanup complete.")

if __name__ == "__main__":

    main()
