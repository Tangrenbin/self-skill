---
name: hplc-xmodem-upgrader
description: Upgrade this CCO device family over the USB log serial port with a `.bin` image using the bootloader image then download 0 XMODEM workflow. Use when the user provides a USB serial device such as `/dev/ttyUSB0` or `/dev/ttyACM0` plus a firmware image path and wants Codex to perform the upgrade, save a serial log, or verify the upgrade environment. Trigger on requests about serial upgrade, USB device names, ttyUSB or ttyACM ports, XMODEM image download, or upgrade package paths.
---

# HPLC XMODEM Upgrader

Use `scripts/serial_upgrade.py` for all real upgrade work. Do not reimplement the serial flow in the chat unless you are debugging the script itself.

## Workflow

1. Normalize the image path before invoking the script. If the user writes a Windows-style relative path like `SDK\make\image.bin`, convert it to a normal workspace path.
2. Confirm the serial device exists and the image file exists.
3. If the user wants a probe or you need quick validation, run:

```bash
python3 /root/.codex/skills/hplc-xmodem-upgrader/scripts/serial_upgrade.py --port /dev/ttyUSB0 --image /abs/path/image.bin --check-only
```

4. For the actual upgrade, run:

```bash
python3 /root/.codex/skills/hplc-xmodem-upgrader/scripts/serial_upgrade.py --port /dev/ttyUSB0 --image /abs/path/image.bin
```

5. Let the script manage the full sequence:
   - detect `@master>>`, `[root /]#`, or `[config /]#`
   - reboot into the bootloader if needed
   - switch baud `115200 -> 460800`
   - enter `image`
   - run `download 0`
   - answer the flash overwrite confirmation
   - send the image by XMODEM
   - wait for `Image download OK!`
   - reboot and capture post-upgrade boot logs at `115200`
6. Report the final result, the log path, and any firmware version/build lines visible in the boot log.

## Expected Prompts

- Application prompt: `@master>>`
- Bootloader prompt: `[root /]#`
- Config prompt: `[config /]#`
- Image prompt: `[image /]#`
- Transfer-ready prompt: `Ctrl+c to cancel`
- Success string: `Image download OK!`

Stop and report if the device does not expose these prompts or if the script reports an unsupported state. Do not keep guessing commands on an unknown target.

## Notes

- The script saves a timestamped log file under `/` by default, for example `/ttyUSB0_upgrade_YYYYMMDD_HHMMSS.log`.
- Pass `--log-path` if the user wants a different destination.
- `--check-only` is read-only. It validates the serial device, image path, and visible prompt without changing flash contents.
- The script expects `pyserial`. If it is missing, install it with `python3 -m pip install --user pyserial`.
- Prefer absolute image paths when possible.
