
# Python Nordic Legacy DFU Tool

A command-line utility to perform **Legacy Device Firmware Updates (DFU)** on Nordic Semiconductor nRF51/nRF52 devices using Python.

This tool is designed to replicate the logic of the official [Nordic Android DFU Library](https://github.com/NordicSemiconductor/Android-DFU-Library), specifically handling the **Buttonless Jump** and **Legacy DFU** protocols. It uses the cross-platform [Bleak](https://github.com/hbldh/bleak) library for Bluetooth Low Energy communication.

## Features

*   **Buttonless DFU:** Automatically switches the device from Application mode to Bootloader mode.
*   **Legacy DFU Protocol:** Supports the standard Nordic Legacy DFU process (SDK < 12 or Adafruit Bootloader).
*   **Zip Support:** Accepts standard firmware `.zip` packages (containing `manifest.json`, `.bin`, and `.dat`).
*   **Cross-Platform:** Works on Windows, macOS, and Linux.
*   **Tunable:** Configurable Packet Receipt Notification (PRN) and transmission delays to handle slower bootloaders.

## Prerequisites

*   Python 3.9 or higher.
*   A Bluetooth Low Energy (BLE) adapter.

## Installation

1.  **Clone or download this repository.**
2.  **Install dependencies:**

```bash
pip install bleak
```

## Usage

```bash
python dfu.py <zip_file> <device_identifier> [options]
```

### Arguments

| Argument | Description |
| :--- | :--- |
| `file` | Path to the `.zip` firmware file. |
| `device` | The BLE name (e.g., `MyDevice`) or MAC Address (e.g., `AA:BB:CC:11:22:33`) of the target. |
| `--scan` | Force a scan for the device even if a MAC address is provided (useful if the device is cached or address type is unknown). |
| `--prn <N>` | Packet Receipt Notification interval. Default is `12`. Set to `0` to disable. Lower values are safer but slower; higher values are faster. |
| `--delay <S>` | **Critical:** Delay in seconds between sending the "Start DFU" command and the "Firmware Size" packet. Default is `0.2`. |
| `--verbose` | Enable debug logging to see detailed BLE traffic. |

### Examples

**1. Basic Update (using Device Name):**
```bash
python dfu.py firmware_package.zip MyDevice
```

**2. Update using MAC Address (Windows/Linux):**
```bash
python dfu.py firmware_package.zip AA:BB:CC:DD:EE:FF
```

**3. Update a slow device (Adafruit/Seeed Bootloaders):**
If you encounter timeouts at the start, increase the delay and lower the PRN:
```bash
python dfu.py firmware.zip MyDevice --delay 0.5 --prn 8
```

**4. macOS Usage:**
On macOS, MAC addresses are hidden. You must use the device name or the specific UUID if known.
```bash
python dfu.py firmware.zip "XIAO_NRF52_OTA" --scan
```

## How it Works

1.  **Zip Parsing:** Extracts the `application.bin` and `application.dat` based on the `manifest.json`.
2.  **Buttonless Jump:** Connects to the device running the application, enables notifications, and sends the "Enter Bootloader" opcode (`0x01, 0x04`).
3.  **Reconnection:** Waits for the device to reboot. It then scans for the device in Bootloader mode (usually advertising as `DFU`, `AdaDFU`, or with the DFU Service UUID).
4.  **DFU Process:**
    *   Sends **Start DFU** command.
    *   Sends **Firmware Size**.
    *   Sends **Init Packet** (`.dat` file).
    *   Streams the **Firmware Image** (`.bin` file).
    *   Validates and resets the device.

## Troubleshooting

### "Timeout waiting for response to Op Code 0x1"
This occurs when the computer sends the firmware size packet before the device has finished processing the "Start" command.
*   **Fix:** Increase the delay using `--delay 0.5` or `--delay 1.0`.

### "Device not found"
*   Ensure the device is advertising.
*   If on Linux, ensure your user has permissions to access the Bluetooth controller.
*   Try using `--scan` to force a fresh discovery.

### "Upload failed" or Stalling
*   Try reducing the PRN value: `--prn 4` or `--prn 1`. This slows down the upload but ensures the device acknowledges packets more frequently.

## Compatibility

Tested with:
*   **Adafruit nRF52 Bootloader** (Used in Adafruit Feather, Seeed XIAO nRF52, RAK4631, etc.).
*   **Nordic SDK 11/12 Legacy Bootloaders**.

*Note: This tool does not support the "Secure DFU" protocol introduced in Nordic SDK 12+. It supports "Legacy DFU" only.*

## License

This utility is a Python implementation based on logic from the open-source Nordic Semiconductor Android DFU Library.

Use at your own risk. Ensure you have recovery mechanisms (e.g., a physical reset button) available when developing firmware updates.
