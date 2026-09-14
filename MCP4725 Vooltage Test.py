import time
from smbus2 import SMBus

I2C_BUS = 1
DAC_ADDR = 0x60

VREF = 3.3
RESOLUTION = 4095

def voltage_to_digital(voltage: float) -> int:
    if voltage < 0 or voltage > VREF:
        raise ValueError(f"Voltage must be between 0 and {VREF} V")
    return int((voltage / VREF) * RESOLUTION)

def write_dac(bus: SMBus, value: int):
    high = (value >> 4) & 0xFF
    low = (value & 0x0F) << 4
    bus.write_i2c_block_data(DAC_ADDR, high, [low])

def main():
    print("Jetson Orin Nano + MCP4725 DAC")
    print("Type a voltage between 0 and 3.3 V.\n")

    bus = SMBus(I2C_BUS)

    try:
        while True:
            try:
                user_input = input("Enter voltage (0–3.3V): ").strip()
                if user_input == "":
                    continue

                voltage = float(user_input)
                digital_value = voltage_to_digital(voltage)

                write_dac(bus, digital_value)

                print(f"Set DAC to {voltage:.3f} V (code {digital_value})\n")
                time.sleep(0.1)

            except ValueError as e:
                print(f"Error: {e}\n")
            except KeyboardInterrupt:
                print("\nExiting...")
                break
    finally:
        bus.close()

if __name__ == "__main__":
    main()
