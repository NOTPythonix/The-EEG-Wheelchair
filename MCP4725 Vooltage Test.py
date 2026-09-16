import smbus2
import time

DAC_ADDR = 0x62
bus = smbus2.SMBus(1)

def voltage_to_digital(voltage, vref=3.3):
    if voltage < 0 or voltage > vref:
        raise ValueError("Voltage must be between 0 and 3.3V")
    return int((voltage / vref) * 4095)

def write_dac(value):
    high = (value >> 8) & 0x0F
    low = value & 0xFF
    bus.write_i2c_block_data(DAC_ADDR, high, [low])

def main():
    print("Jetson Orin Nano MCP4725 Voltage Output Tool")
    print("Type a voltage between 0 and 3.3V.\n")

    while True:
        try:
            user_input = input("Enter voltage (0–3.3V): ")
            voltage = float(user_input)

            digital_value = voltage_to_digital(voltage)
            write_dac(digital_value)

            print(f"Output set to {voltage:.3f} V")
            print("Measure VOUT with your multimeter.\n")

        except ValueError as e:
            print(f"Error: {e}\n")
        except KeyboardInterrupt:
            print("\nExiting...")
            break

if __name__ == "__main__":
    main()
