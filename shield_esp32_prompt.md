# SHIELD ESP32-S3 Firmware — Prompt

Write ESP-IDF (v5.x, C) firmware for an ESP32-S3-DevKitC-1 acting as the sensor node of S.H.I.E.L.D.

## Hardware / Pins
| Function | Pin |
|---|---|
| PIR (HC-SR501, L mode) | IO4, GPIO interrupt |
| mmWave radar UART1 (assume LD2410, 256000 baud) | TX IO15, RX IO16 |
| mmWave OUT (presence) | IO5 |
| Buzzer (LEDC PWM, 2 kHz) | IO17 |
| UART2 link to RPi | TX IO43, RX IO44 |

Console logging goes over USB-Serial-JTAG, not UART0 (IO43/44 are used for the RPi link).

## Behavior
- Trigger = PIR interrupt OR mmWave presence. Debounce 200 ms. Require both within 2 s for a high-confidence trigger; either alone is a low-confidence trigger.
- On trigger, send RPi packet: `0xAA | 0x01 | timestamp 4B big-endian | XOR checksum` (7 bytes).
- Parse RPi reply: `0xBB | status (0 clear, 1 known, 2 unknown/alert) | confidence 0-100 | XOR checksum` (4 bytes). Reject bad checksums.
- Status 2: buzzer alarm pattern (LEDC), 10 s. Status 0/1: stay silent, re-arm.
- If no reply in 5 s: retry once, then fail-safe to alarm.
- Cooldown 10 s after each event.

## Structure
- FreeRTOS tasks: `sensor_task`, `radar_task` (LD2410 frame parser), `uart_link_task`, `buzzer_task`, with queues between them.
- Components: `pir`, `ld2410`, `rpi_link`, `buzzer`, each with its own header.
- Task watchdog enabled.
- Kconfig options for pins, timeouts, cooldown.

## Deliverables
`CMakeLists.txt`, `sdkconfig.defaults`, all source files, and a short README with build/flash steps. Include a host-side unit test for the packet encode/decode and checksum.
