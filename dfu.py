#!/usr/bin/env python3

import asyncio
import argparse
import logging
import sys
import os
import struct
import zipfile
import json
import time
from typing import Optional

from bleak import BleakScanner, BleakClient, BleakError
from bleak.backends.device import BLEDevice

# --- UUID Constants ---
DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"
DFU_VERSION_UUID = "00001534-1212-efde-1523-785feabcd123"

# --- Op Codes ---
OP_CODE_START_DFU = 0x01
OP_CODE_INIT_DFU_PARAMS = 0x02
OP_CODE_RECEIVE_FIRMWARE_IMAGE = 0x03
OP_CODE_VALIDATE = 0x04
OP_CODE_ACTIVATE_AND_RESET = 0x05
OP_CODE_RESET = 0x06
OP_CODE_PACKET_RECEIPT_NOTIF_REQ = 0x08
OP_CODE_RESPONSE_CODE = 0x10
OP_CODE_PACKET_RECEIPT_NOTIF = 0x11
OP_CODE_ENTER_BOOTLOADER = 0x01
UPLOAD_MODE_APPLICATION = 0x04

# --- Custom Logger ---
class MsFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        t = time.strftime("%H:%M:%S", ct)
        return f"{t}.{int(record.msecs):03d}"

    def format(self, record):
        timestamp = self.formatTime(record)
        msg = record.getMessage()
        return f"{timestamp}  {msg}"

logger = logging.getLogger("DFU")

class DfuException(Exception):
    pass

class NordicLegacyDFU:
    def __init__(self, zip_path: str, prn: int, packet_delay: float, adapter: str = None):
        self.zip_path = zip_path
        self.prn = prn
        self.packet_delay = packet_delay
        self.adapter = adapter

        self.manifest = None
        self.bin_data = None
        self.dat_data = None
        self.client: Optional[BleakClient] = None

        self.response_queue = asyncio.Queue()
        self.pkg_receipt_event = asyncio.Event()
        self.bytes_sent = 0

    def parse_zip(self):
        if not os.path.exists(self.zip_path):
            raise FileNotFoundError(f"File not found: {self.zip_path}")

        with zipfile.ZipFile(self.zip_path, 'r') as z:
            if 'manifest.json' in z.namelist():
                with z.open('manifest.json') as f:
                    self.manifest = json.load(f)

                if 'manifest' in self.manifest and 'application' in self.manifest['manifest']:
                    app_info = self.manifest['manifest']['application']
                    self.bin_data = z.read(app_info['bin_file'])
                    self.dat_data = z.read(app_info['dat_file'])
                else:
                    raise DfuException("Zip must contain an Application firmware manifest.")
            else:
                logger.info("No manifest.json. Attempting legacy compatibility mode.")
                files = z.namelist()
                bin_file = next((f for f in files if f.endswith('.bin') and 'application' in f.lower()), None)
                dat_file = next((f for f in files if f.endswith('.dat') and 'application' in f.lower()), None)

                if bin_file and dat_file:
                    self.bin_data = z.read(bin_file)
                    self.dat_data = z.read(dat_file)
                else:
                    raise DfuException("Could not auto-detect firmware files in ZIP.")

    async def _notification_handler(self, sender, data):
        data = bytearray(data)
        opcode = data[0]

        if opcode == OP_CODE_RESPONSE_CODE:
            request_op = data[1]
            status = data[2]
            logger.debug(f"<< RX Resp: Op={request_op:#02x} Status={status}")
            await self.response_queue.put((request_op, status))

        elif opcode == OP_CODE_PACKET_RECEIPT_NOTIF:
            if len(data) >= 5:
                bytes_received = struct.unpack('<I', data[1:5])[0]
                logger.debug(f"<< RX PRN: {bytes_received}")
            self.pkg_receipt_event.set()

    async def _wait_for_response(self, expected_op_code, timeout=30.0):
        try:
            request_op, status = await asyncio.wait_for(self.response_queue.get(), timeout)
            if request_op != expected_op_code:
                logger.debug(f"Ignored unexpected response: {request_op:#02x}")
                # In a real scenario we might loop here, but keeping it simple
                return -1

            if status != 1: # 1 = SUCCESS
                logger.error(f"<< RX Error: Command {expected_op_code:#02x} failed with status {status}")
                return status

            return 1
        except asyncio.TimeoutError:
            logger.error(f"Timeout ({timeout}s) waiting for response to Op Code {expected_op_code:#02x}")
            return -1

    async def jump_to_bootloader(self, device: BLEDevice):
        logger.info(f"Connecting to {device.name} ({device.address}) for Jump...")
        try:
            async with BleakClient(device, adapter=self.adapter) as client:
                await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                payload = bytearray([OP_CODE_ENTER_BOOTLOADER, UPLOAD_MODE_APPLICATION])

                logger.debug(f">> TX Jump: {payload.hex()}")
                try:
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, payload, response=True)
                except Exception:
                    pass
                logger.info("Jump command sent.")
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.info(f"Jump connection sequence ended (likely success): {e}")

    async def perform_update(self, device: BLEDevice):
        logger.info(f"Target Bootloader: {device.address}")

        max_retries = 3
        for attempt in range(max_retries):
            logger.info(f"DFU connection attempt {attempt+1}/{max_retries}...")

            try:
                async with BleakClient(device, timeout=20.0, adapter=self.adapter) as client:
                    self.client = client

                    # --- STABILIZE ---
                    await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                    while not self.response_queue.empty(): self.response_queue.get_nowait()

                    # --- STEP 1: START DFU ---
                    start_payload = bytearray([OP_CODE_START_DFU, UPLOAD_MODE_APPLICATION])
                    logger.debug(f">> TX Start DFU: {start_payload.hex()}")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, start_payload, response=True)

                    # Wait for device state switch
                    if self.packet_delay > 0:
                        logger.debug(f"Pausing {self.packet_delay}s for device state switch...")
                        await asyncio.sleep(self.packet_delay)

                    sd_size = 0
                    bl_size = 0
                    app_size = len(self.bin_data)
                    size_payload = struct.pack('<III', sd_size, bl_size, app_size)

                    logger.info(f"Sending Size: {app_size} bytes")
                    logger.debug(f">> TX Size: {size_payload.hex()}")

                    await client.write_gatt_char(DFU_PACKET_UUID, size_payload, response=False)

                    # CRITICAL: This verifies the flash area. On this device it takes ~17s.
                    # We set timeout to 60s to be safe on slower adapters.
                    status = await self._wait_for_response(OP_CODE_START_DFU, timeout=60.0)

                    if status != 1:
                        logger.warning(f"Start DFU failed (Status {status}). Resetting...")
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RESET]), response=True)
                        raise DfuException("Start DFU sequence failed")

                    # --- STEP 2: INIT PACKET ---
                    logger.info("Sending Init Packet...")

                    logger.debug(">> TX Init Start")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x00]), response=True)

                    logger.debug(f">> TX Init Data: {len(self.dat_data)} bytes")
                    await client.write_gatt_char(DFU_PACKET_UUID, self.dat_data, response=False)

                    logger.debug(">> TX Init End")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x01]), response=True)

                    status = await self._wait_for_response(OP_CODE_INIT_DFU_PARAMS)
                    if status != 1: raise DfuException(f"Init Packet failed. Status: {status}")

                    # --- STEP 3: CONFIGURE PRN ---
                    if self.prn > 0:
                        logger.info(f"Configuring PRN: {self.prn}")
                        prn_payload = bytearray([OP_CODE_PACKET_RECEIPT_NOTIF_REQ]) + struct.pack('<H', self.prn)
                        logger.debug(f">> TX PRN: {prn_payload.hex()}")
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, prn_payload, response=True)

                    # --- STEP 4: RECEIVE FIRMWARE IMAGE ---
                    logger.info("Requesting Upload...")
                    logger.debug(">> TX Receive FW")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RECEIVE_FIRMWARE_IMAGE]), response=True)

                    # --- STEP 5: STREAM BINARY ---
                    await self._stream_firmware()

                    # --- STEP 6: CHECK UPLOAD STATUS ---
                    logger.info("Verifying Upload...")
                    status = await self._wait_for_response(OP_CODE_RECEIVE_FIRMWARE_IMAGE)
                    if status != 1: raise DfuException(f"Upload failed. Status: {status}")

                    # --- STEP 7: VALIDATE ---
                    logger.info("Validating...")
                    logger.debug(">> TX Validate")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_VALIDATE]), response=True)
                    status = await self._wait_for_response(OP_CODE_VALIDATE)
                    if status != 1: raise DfuException(f"Validation failed. Status: {status}")

                    # --- STEP 8: ACTIVATE AND RESET ---
                    logger.info("Activating & Resetting...")
                    try:
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_ACTIVATE_AND_RESET]), response=True)
                    except Exception:
                        pass

                    logger.info("DFU Complete.")
                    return

            except Exception as e:
                logger.error(f"Attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    logger.info("Retrying in 3s...")
                    await asyncio.sleep(3.0)
                else:
                    logger.error("Max retries reached.")
                    sys.exit(1)

    async def _stream_firmware(self):
        chunk_size = 20
        total_bytes = len(self.bin_data)
        packets_since_prn = 0
        self.bytes_sent = 0

        logger.info(f"Uploading {total_bytes} bytes...")

        for i in range(0, total_bytes, chunk_size):
            chunk = self.bin_data[i : i + chunk_size]

            await self.client.write_gatt_char(DFU_PACKET_UUID, chunk, response=False)
            self.bytes_sent += len(chunk)
            packets_since_prn += 1

            if i % 2000 == 0:
                pct = int((self.bytes_sent / total_bytes) * 100)
                sys.stdout.write(f"\rUploading: {pct}%")
                sys.stdout.flush()

            if self.prn > 0 and packets_since_prn >= self.prn:
                self.pkg_receipt_event.clear()
                try:
                    await asyncio.wait_for(self.pkg_receipt_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("\nPRN Timeout, continuing anyway...")

                packets_since_prn = 0

        sys.stdout.write("\rUploading: 100%\n")
        sys.stdout.flush()

async def find_device(name_or_address: str, force_scan: bool, adapter: str = None, service_uuid: str = None) -> BLEDevice:
    logger.info(f"Scanning for {name_or_address}...")

    if not force_scan and not adapter:
        try:
            device = await BleakScanner.find_device_by_address(name_or_address, timeout=10.0)
            if device:
                return device
        except BleakError:
            pass

    scanner = BleakScanner(adapter=adapter)
    scanned_devices = await scanner.discover(timeout=5.0, return_adv=True)

    target = None

    for key, (d, adv) in scanned_devices.items():
        if d.address.upper() == name_or_address.upper():
            target = d; break
        adv_name = adv.local_name or d.name or ""
        if adv_name == name_or_address:
            target = d; break
        if not target and service_uuid:
            if service_uuid.lower() in [u.lower() for u in adv.service_uuids]:
                target = d; break

    if not target:
        raise DfuException("Device not found.")

    return target

async def main():
    parser = argparse.ArgumentParser(description="Nordic Semi Buttonless Legacy DFU Utility")
    parser.add_argument("file", help="Path to the ZIP firmware file")
    parser.add_argument("device", help="Device Name or BLE Address")
    parser.add_argument("--scan", action="store_true", help="Force scan even if address is provided")
    parser.add_argument("--adapter", default=None, help="Bluetooth Adapter interface (Linux: hci0)")
    parser.add_argument("--prn", type=int, default=8, help="PRN interval (default 8)")
    parser.add_argument("--delay", type=float, default=0.4, help="Start/Size Delay (default 0.4s)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logs")

    args = parser.parse_args()

    handler = logging.StreamHandler()
    if args.verbose:
        handler.setFormatter(MsFormatter())
        logger.setLevel(logging.DEBUG)
        logging.getLogger("bleak").setLevel(logging.WARNING)
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.setLevel(logging.INFO)
        logging.getLogger("bleak").setLevel(logging.ERROR)

    logger.addHandler(handler)
    logger.propagate = False

    try:
        dfu = NordicLegacyDFU(args.file, args.prn, args.delay, adapter=args.adapter)
        dfu.parse_zip()

        app_device = await find_device(args.device, args.scan, adapter=args.adapter)
        await dfu.jump_to_bootloader(app_device)

        logger.info("Waiting for reboot (5s)...")
        await asyncio.sleep(5.0)

        bootloader_device = None
        try:
            logger.info("Scanning for Bootloader (UUID)...")
            bootloader_device = await find_device("DFU", force_scan=True, adapter=args.adapter, service_uuid=DFU_SERVICE_UUID)
        except DfuException:
            pass

        if not bootloader_device:
            original_mac = app_device.address
            if ":" in original_mac and len(original_mac) == 17:
                try:
                    prefix = original_mac[:-2]
                    last_byte = int(original_mac[-2:], 16)
                    last_byte = (last_byte + 1) & 0xFF
                    bootloader_mac_hint = f"{prefix}{last_byte:02X}"
                    logger.info(f"Scanning for Bootloader (Hint: {bootloader_mac_hint})...")
                    bootloader_device = await find_device(bootloader_mac_hint, force_scan=True, adapter=args.adapter)
                except: pass

        if not bootloader_device:
            raise DfuException("Could not locate DFU Bootloader device.")

        await dfu.perform_update(bootloader_device)

    except Exception as e:
        logger.error(f"Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())