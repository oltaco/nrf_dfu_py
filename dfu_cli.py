#!/usr/bin/env python3
# --- START OF FILE dfu_cli.py ---

import asyncio
import argparse
import logging
import sys
import time

# Update import to include the new find_any_device function
from dfu_lib import NordicLegacyDFU, fast_find_bootloader, find_any_device, find_device_by_name_or_address, DfuException, DFU_SERVICE_UUID, suppress_invalid_state

# --- Custom Logger for CLI ---
class MsFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        t = time.strftime("%H:%M:%S", ct)
        return f"{t}.{int(record.msecs):03d}"

    def format(self, record):
        timestamp = self.formatTime(record)
        msg = record.getMessage()
        return f"{timestamp}  {msg}"

logger = logging.getLogger("DFU_CLI")

def cli_progress_handler(pct):
    sys.stdout.write(f"\rUploading: {pct}%")
    sys.stdout.flush()
    if pct == 100:
        sys.stdout.write("\n")

async def main():
    parser = argparse.ArgumentParser(description="Nordic Semi Buttonless Legacy DFU Utility (CLI)")
    parser.add_argument("file", help="Path to the ZIP firmware file")

    # Changed: nargs='+' allows multiple arguments to be collected into a list
    parser.add_argument("device", nargs='+', help="Device Name(s) or BLE Address(es). You can provide multiple.")

    parser.add_argument("--scan", action="store_true", help="Force scan even if address is provided")
    parser.add_argument("--adapter", default=None, help="Bluetooth Adapter interface (Linux: hci0)")
    parser.add_argument("--prn", type=int, default=8, help="PRN interval (default 8)")
    parser.add_argument("--delay", type=float, default=0.0, help="Start/Size Delay (default 0.0s)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logs")

    # New Arguments
    parser.add_argument("--wait", action="store_true", help="Loop indefinitely until one of the target devices is found")
    parser.add_argument("--retry", type=int, default=3, help="Number of DFU connection retries (default 3)")
    
    # Arguments for stress-testing
    parser.add_argument("--loop", action="store_true", help="Run DFU indefinitely with 60s delay between runs")
    parser.add_argument("--log-file", default=None, help="Log output to file")
    parser.add_argument("--corrupt", action="store_true", help="Intentionally corrupt some packets to test CRC failure handling")


    args = parser.parse_args()
    
    # Suppress Bleak's straggling callback errors
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(suppress_invalid_state)


    handler = logging.StreamHandler()
    if args.verbose:
        handler.setFormatter(MsFormatter())
        logger.setLevel(logging.DEBUG)
        logging.getLogger("bleak").setLevel(logging.WARNING)
        logging.getLogger("DFU_LIB").setLevel(logging.DEBUG)
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.setLevel(logging.INFO)
        logging.getLogger("bleak").setLevel(logging.ERROR)
        logging.getLogger("DFU_LIB").setLevel(logging.INFO)

    logger.addHandler(handler)
    logging.getLogger("DFU_LIB").addHandler(handler) # Attach handler to lib logger
    
    # Add file handler if specified
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)
        logging.getLogger("DFU_LIB").addHandler(file_handler)
        
    run_count = 0
    while True:
        run_count += 1
        logger.info(f"=== DFU Run #{run_count} ===")


        try:
            # Pass None for log_callback so the library uses the standard logger configured above
            dfu = NordicLegacyDFU(args.file, args.prn, args.delay, adapter=args.adapter, progress_callback=cli_progress_handler)
            dfu.corrupt_packets = args.corrupt
            dfu.parse_zip()

            logger.info(f"Scanning for target(s): {args.device}...")

            # --- WAIT / SCAN Loop ---
            app_device = None
            while True:
                try:
                    # Use find_any_device to check all inputs in a single scan cycle
                    app_device = await find_any_device(args.device, adapter=args.adapter)
                    logger.info(f"Found target: {app_device.name} ({app_device.address})")
                    break # Found!
                except DfuException:
                    if args.wait:
                        logger.info("No devices found. Retrying scan...")
                        await asyncio.sleep(2.0)
                        continue
                    else:
                        logger.error(f"Could not find any of: {args.device}")
                        raise

            for attempt in range(3):
                try:
                    if attempt > 0:
                        # Re-scan for fresh device info
                        logger.info("Re-scanning for device...")
                        app_device = await find_any_device(args.device, adapter=args.adapter)
                    await dfu.jump_to_bootloader(app_device)
                    break
                except DfuException as e:
                    if attempt < 2:
                        logger.warning(f"Retrying jump... ({attempt + 1}/3)")
                        await asyncio.sleep(2.0)
                    else:
                        raise

            logger.info("Waiting for reboot...(1s)")
            await asyncio.sleep(1.0)
        
            bootloader_device = None
            try:
                logger.info("Scanning for Bootloader (UUID)...")
                bootloader_device = await fast_find_bootloader(adapter=args.adapter, service_uuid=DFU_SERVICE_UUID)

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
                        bootloader_device = await find_device_by_name_or_address(bootloader_mac_hint, force_scan=True, adapter=args.adapter)
                    except: pass

            if not bootloader_device:
                raise DfuException("Could not locate DFU Bootloader device.")

            # Pass the custom retry count here
            #await dfu.perform_update(bootloader_device, max_retries=args.retry)
            try:
                await asyncio.wait_for(dfu.perform_update(bootloader_device, max_retries=args.retry), timeout=300.0)
            except asyncio.TimeoutError:
                raise DfuException("DFU timed out after 5 minutes")

            
            logger.info(f"Run #{run_count} completed successfully")


        except KeyboardInterrupt:
            logger.info("\nOperation Cancelled by User.")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Run #{run_count} failed: {e}")

            logger.error(f"Failed: {e}")      
        if not args.loop:
            break
            
        logger.info("Waiting 60s before next run...")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())