#!/usr/bin/env python3

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required. Install it with: python3 -m pip install --user pyserial"
    ) from exc


SOH = 0x01
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQ = ord("C")
SUB = 0x1A


class UpgradeError(RuntimeError):
    pass


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_user_path(raw_path: str) -> Path:
    normalized = raw_path.replace("\\", "/")
    return Path(normalized).expanduser().resolve()


def default_log_path(port_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(port_name.rstrip("/")) or "serial"
    return f"/{base}_upgrade_{stamp}.log"


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def sanitize_bytes(data: bytes) -> str:
    out = []
    for byte in data:
        if byte in (9, 10, 13):
            out.append(chr(byte))
        elif 32 <= byte <= 126:
            out.append(chr(byte))
        elif byte == 27:
            out.append("<ESC>")
        else:
            out.append(f"<0x{byte:02X}>")
    return "".join(out)


class UpgradeSession:
    def __init__(self, port: str, image_path: Path, log_path: str, post_boot_seconds: float):
        self.port = port
        self.image_path = image_path
        self.log_path = log_path
        self.post_boot_seconds = post_boot_seconds
        self.image_size = image_path.stat().st_size
        self.ser = None
        Path(log_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(log_path, "w", encoding="utf-8", buffering=1)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.log_file.close()

    def record(self, message: str):
        line = message if message.endswith("\n") else message + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        self.log_file.write(line)
        self.log_file.flush()

    def log_rx(self, data: bytes):
        if not data:
            return
        text = sanitize_bytes(data)
        sys.stdout.write(text)
        sys.stdout.flush()
        self.log_file.write(text)
        self.log_file.flush()

    def open_serial(self, baudrate: int):
        self.ser = serial.Serial(
            self.port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=1,
        )

    def write_ascii(self, payload: str, label: str):
        self.record(f"[{now_ts()}] TX @ {self.ser.baudrate}: {label}")
        self.ser.write(payload.encode("ascii"))
        self.ser.flush()

    def read_for(self, seconds: float) -> bytes:
        deadline = time.time() + seconds
        buf = bytearray()
        while time.time() < deadline:
            data = self.ser.read(4096)
            if data:
                buf.extend(data)
                self.log_rx(data)
            else:
                time.sleep(0.02)
        return bytes(buf)

    def wait_for(self, patterns, timeout: float, periodic_callback=None, interval: float = 1.0):
        if isinstance(patterns, (bytes, str)):
            patterns = [patterns]
        encoded = [p.encode("latin1") if isinstance(p, str) else p for p in patterns]
        deadline = time.time() + timeout
        tail = bytearray()
        next_tick = time.time() + interval if periodic_callback else None
        while time.time() < deadline:
            data = self.ser.read(4096)
            if data:
                tail.extend(data)
                if len(tail) > 65536:
                    del tail[:-8192]
                self.log_rx(data)
                for index, pattern in enumerate(encoded):
                    if pattern in tail:
                        return index, bytes(tail)
            else:
                time.sleep(0.02)
            if periodic_callback and time.time() >= next_tick:
                periodic_callback()
                next_tick = time.time() + interval
        return -1, bytes(tail)

    def detect_state(self, baudrate: int):
        self.open_serial(baudrate)
        self.ser.reset_input_buffer()
        self.write_ascii("\r\n\r\n", "<CRLF>")
        text = self.read_for(1.0).decode("latin1", "ignore")
        if "[config /]#" in text:
            return "config"
        if "[root /]#" in text:
            return "root"
        if "@master>>" in text:
            return "master"
        return None

    def ensure_root_prompt(self):
        for baudrate in (460800, 115200):
            state = None
            try:
                state = self.detect_state(baudrate)
                if state:
                    self.record(f"[{now_ts()}] detected state={state} at baud={baudrate}")
                if state == "root":
                    return
                if state == "config":
                    self.write_ascii("exit\r\n", "exit")
                    found, _ = self.wait_for("[root /]#", 2)
                    if found >= 0:
                        return
                    raise UpgradeError("left [config /]# but did not reach [root /]#")
                if state == "master":
                    self.record(f"[{now_ts()}] rebooting from @master prompt")
                    self.write_ascii("\r\n\r\nreboot\r\n", "reboot")
                    sent_d = False
                    deadline = time.time() + 20
                    tail = bytearray()
                    while time.time() < deadline:
                        data = self.ser.read(4096)
                        if data:
                            tail.extend(data)
                            if len(tail) > 65536:
                                del tail[:-8192]
                            self.log_rx(data)
                            if not sent_d and (b"ST  OK" in tail or b"MHz" in tail):
                                self.record(f"[{now_ts()}] boot banner detected, sending d x20")
                                for _ in range(20):
                                    self.ser.write(b"d\r\n")
                                    self.ser.flush()
                                    time.sleep(0.01)
                                sent_d = True
                            if b"[root /]#" in tail:
                                return
                        else:
                            time.sleep(0.02)
                    raise UpgradeError("rebooted but did not reach [root /]#")
            finally:
                if state is None and self.ser is not None:
                    self.ser.close()
                    self.ser = None
        raise UpgradeError("unable to detect @master>>, [root /]#, or [config /]# at 460800/115200")

    def switch_root_to_460800(self):
        if self.ser.baudrate == 460800:
            self.record(f"[{now_ts()}] already at 460800")
            return
        self.write_ascii("config\r\n", "config")
        found, _ = self.wait_for("[config /]#", 2)
        if found < 0:
            raise UpgradeError("failed to enter [config /]#")
        self.write_ascii("setbaudrate 460800\r\n", "setbaudrate 460800")
        self.read_for(1.0)
        self.record(f"[{now_ts()}] switching host baud 115200 -> 460800")
        self.ser.baudrate = 460800
        self.ser.reset_input_buffer()
        self.write_ascii("\r\n\r\n\r\n", "<CRLF>")
        found, _ = self.wait_for("[config /]#", 2)
        if found < 0:
            raise UpgradeError("host switched to 460800 but [config /]# was not observed")
        self.write_ascii("exit\r\n", "exit")
        found, _ = self.wait_for("[root /]#", 2)
        if found < 0:
            raise UpgradeError("failed to return to [root /]# after setbaudrate")

    def xmodem_send(self):
        self.record(f"[{now_ts()}] starting XMODEM transfer")
        start_mode = None
        cancel_seen = False
        deadline = time.time() + 60
        while time.time() < deadline:
            char = self.ser.read(1)
            if not char:
                continue
            value = char[0]
            if value == CRC_REQ:
                start_mode = "crc"
                break
            if value == NAK:
                start_mode = "checksum"
                break
            if value == CAN:
                if cancel_seen:
                    raise UpgradeError("transfer canceled by target during XMODEM start")
                cancel_seen = True
                continue
        if start_mode is None:
            raise UpgradeError("timed out waiting for XMODEM receiver handshake")

        packet_size = 128
        seq = 1
        sent_bytes = 0
        last_percent = -1
        with self.image_path.open("rb") as stream:
            while True:
                chunk = stream.read(packet_size)
                if not chunk:
                    break
                payload = chunk.ljust(packet_size, bytes([SUB]))
                packet = bytearray([SOH, seq & 0xFF, 0xFF - (seq & 0xFF)])
                packet.extend(payload)
                if start_mode == "crc":
                    crc = crc16_xmodem(payload)
                    packet.extend([(crc >> 8) & 0xFF, crc & 0xFF])
                else:
                    packet.append(sum(payload) & 0xFF)

                retries = 0
                while True:
                    self.ser.write(packet)
                    self.ser.flush()
                    response = self.ser.read(1)
                    if response and response[0] == ACK:
                        sent_bytes += len(chunk)
                        percent = int((sent_bytes * 100) / self.image_size)
                        if percent >= last_percent + 5 or sent_bytes == self.image_size:
                            self.record(
                                f"[{now_ts()}] XMODEM progress: {sent_bytes}/{self.image_size} bytes ({percent}%)"
                            )
                            last_percent = percent
                        seq = (seq + 1) % 256
                        break
                    if response and response[0] == CAN:
                        raise UpgradeError("transfer canceled by target during data send")
                    retries += 1
                    if retries > 16:
                        raise UpgradeError("too many XMODEM retries while sending data")

        retries = 0
        while retries <= 16:
            self.ser.write(bytes([EOT]))
            self.ser.flush()
            response = self.ser.read(1)
            if response and response[0] == ACK:
                self.record(f"[{now_ts()}] XMODEM finished successfully")
                return
            retries += 1
        raise UpgradeError("target did not ACK XMODEM EOT")

    def run_check_only(self):
        detected = []
        for baudrate in (460800, 115200):
            if self.ser is not None:
                self.ser.close()
                self.ser = None
            state = self.detect_state(baudrate)
            detected.append((baudrate, state))
            self.record(f"[{now_ts()}] check-only: baud={baudrate}, state={state}")
            self.ser.close()
            self.ser = None
            if state:
                return
        raise UpgradeError(
            f"check-only failed: no known prompt detected. Results: {detected}"
        )

    def run_upgrade(self):
        self.record(f"[{now_ts()}] starting upgrade on {self.port}")
        self.record(f"[{now_ts()}] image file: {self.image_path} ({self.image_size} bytes)")
        self.record(f"[{now_ts()}] log file: {self.log_path}")
        self.ensure_root_prompt()
        self.switch_root_to_460800()

        self.write_ascii("image\r\n", "image")
        found, _ = self.wait_for("[image /]#", 3)
        if found < 0:
            raise UpgradeError("failed to enter [image /]#")

        self.write_ascii("download 0\r\n", "download 0")
        found, _ = self.wait_for("Warning:", 5)
        if found < 0:
            raise UpgradeError("download 0 did not produce the flash overwrite warning")

        self.write_ascii("Y\r\n", "Y")

        def resend_yes():
            self.write_ascii("Y\r\n", "Y")

        found, _ = self.wait_for("Ctrl+c to cancel", 15, periodic_callback=resend_yes)
        if found < 0:
            raise UpgradeError("did not observe XMODEM start prompt")

        self.xmodem_send()

        found, _ = self.wait_for(["Image download OK", "Image download failed!"], 300)
        if found == 0:
            self.record(f"[{now_ts()}] device reported Image download OK")
        elif found == 1:
            raise UpgradeError("device reported Image download failed!")
        else:
            raise UpgradeError("timed out waiting for Image download OK/failed")

        self.write_ascii("reboot\r\n", "reboot")
        time.sleep(0.2)
        self.record(f"[{now_ts()}] switching host baud 460800 -> 115200 for post-upgrade logs")
        self.ser.baudrate = 115200
        self.read_for(self.post_boot_seconds)
        self.record(f"[{now_ts()}] upgrade completed successfully")
        self.record(f"[{now_ts()}] final log file: {self.log_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upgrade a CCO device over the USB log serial port using XMODEM."
    )
    parser.add_argument("--port", required=True, help="Serial device path, for example /dev/ttyUSB0")
    parser.add_argument("--image", required=True, help="Path to the .bin image file")
    parser.add_argument(
        "--log-path",
        help="Optional log file destination. Default: /<port>_upgrade_<timestamp>.log",
    )
    parser.add_argument(
        "--post-boot-seconds",
        type=float,
        default=10.0,
        help="How long to capture boot logs after reboot. Default: 10",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only probe the serial prompt. Do not reboot, change baud, or write flash.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    port_path = args.port
    image_path = normalize_user_path(args.image)
    if not os.path.exists(port_path):
        raise SystemExit(f"serial device does not exist: {port_path}")
    if not image_path.exists():
        raise SystemExit(f"image file does not exist: {image_path}")
    if image_path.suffix.lower() != ".bin":
        raise SystemExit(f"image file must be a .bin file: {image_path}")

    log_path = str(normalize_user_path(args.log_path)) if args.log_path else default_log_path(port_path)
    session = UpgradeSession(
        port=port_path,
        image_path=image_path,
        log_path=log_path,
        post_boot_seconds=args.post_boot_seconds,
    )
    try:
        if args.check_only:
            session.record(f"[{now_ts()}] check-only probe on {port_path}")
            session.record(f"[{now_ts()}] image file: {image_path} ({image_path.stat().st_size} bytes)")
            session.record(f"[{now_ts()}] log file: {log_path}")
            session.run_check_only()
            session.record(f"[{now_ts()}] check-only probe completed successfully")
        else:
            session.run_upgrade()
    except UpgradeError as exc:
        session.record(f"[{now_ts()}] ERROR: {exc}")
        raise SystemExit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
