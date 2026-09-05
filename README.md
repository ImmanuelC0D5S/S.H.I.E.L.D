# S.H.I.E.L.D
> PIR-triggered security system — STM32 RTOS core, ESP32 WiFi bridge, Python OpenCV on PC. Alerts to phone.

---

## Overview

S.H.I.E.L.D is a bare-metal embedded security system built around an STM32F446RE and ESP32. A PIR sensor detects motion, the STM32 handles all real-time logic via FreeRTOS, the ESP32 bridges to WiFi, and a Python script on a PC processes the camera feed and fires an alert to your phone.

No HAL. No Arduino. Just registers, RTOS, and a working system.

---

## Architecture

```
PIR Sensor → STM32F446RE (FreeRTOS) → UART → ESP32 → WiFi → PC (Python + OpenCV)
                                                                      ↓
                                                             Phone camera stream
                                                                      ↓
                                                             Inference result
                                                                      ↓
                                               ESP32 ← WiFi ← result → STM32 → Buzzer / LED
```

---

## Hardware

| Component | Role |
|---|---|
| STM32F446RE (NUCLEO) | RTOS core — EXTI, FreeRTOS tasks, UART TX, PWM |
| ESP32 | WiFi bridge — UART RX, HTTP/MQTT to PC |
| HC-SR501 PIR | Motion detection trigger |
| Buzzer | Alert output via PWM |
| Phone (IP camera app) | Camera feed to PC |

---

## Concepts Covered

- Bare-metal STM32 register-level programming (no HAL)
- EXTI interrupts and NVIC configuration
- FreeRTOS — tasks, queues, mutexes
- DMA-driven UART TX/RX
- Timer (TIM2) as free-running counter for timestamping
- PWM output for buzzer
- ESP32 WiFi + MQTT (ESP-IDF)
- Python OpenCV — MJPEG stream capture and inference
- Phone → PC alert pipeline (Telegram Bot / ntfy.sh)

---

## Repo Structure

```
S.H.I.E.L.D/
├── firmware/       # STM32CubeIDE project (bare-metal C)
├── esp32/          # ESP-IDF project
├── pc/             # Python OpenCV + alert script
└── docs/           # Wiring diagrams, register notes
```

---

## Firmware Modules (STM32)

| Module | Status |
|---|---|
| RCC + GPIO init | 🔲 |
| EXTI — PIR interrupt | 🔲 |
| TIM2 — timestamping | 🔲 |
| FreeRTOS task architecture | 🔲 |
| DMA UART TX | 🔲 |
| PWM buzzer | 🔲 |

---

## Getting Started

### STM32
1. Open `firmware/` in STM32CubeIDE
2. Build and flash to NUCLEO-F446RE
3. Wire PIR OUT → PA0, buzzer → PA6

### ESP32
1. Set up ESP-IDF environment
2. Configure WiFi credentials in `esp32/main/config.h`
3. Flash to ESP32

### PC
```bash
cd pc/
pip install -r requirements.txt
python shield.py
```

---

## Wiring

| PIR Pin | NUCLEO Pin |
|---|---|
| VCC | 5V (CN7 pin 18) |
| GND | GND (CN7 pin 20) |
| OUT | PA0 (CN7 pin 28) |

---

## Built With

- STM32CubeIDE
- ESP-IDF
- Python 3.x + OpenCV
- FreeRTOS (CMSIS-RTOS v2)

---

## Author

Immanuel Augustine — [@ImmanuelC0D5S](https://github.com/ImmanuelC0D5S)