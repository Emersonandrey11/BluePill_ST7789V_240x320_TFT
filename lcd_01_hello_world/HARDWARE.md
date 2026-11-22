# Hardware

Board: STM32F103C8T6 "Blue Pill" (8 MHz crystal, 64 KB flash, 20 KB RAM).
Programmed over SWD with an ST-Link V2 (SWDIO, SWCLK, GND, 3.3 V).

## 2.8" ST7789V TFT (240x320, SPI)

| TFT pin    | Blue Pill | Use                                             |
|------------|-----------|-------------------------------------------------|
| SCK        | PA5       | SPI1_SCK, alternate function push-pull          |
| SDI (MOSI) | PA7       | SPI1_MOSI, alternate function push-pull         |
| SDO (MISO) | PA6       | SPI1_MISO, input (optional: panel ID read only) |
| CS         | PA4       | GPIO output, active low                         |
| DC         | PA3       | GPIO output, low = command, high = data         |
| RESET      | PA2       | GPIO output, active low                         |
| LED        | PB0       | backlight, TIM3_CH3 PWM at 20 kHz               |
| VCC        | 3.3 V     |                                                 |
| GND        | GND       |                                                 |

## On-board

- PC13: LED (active low)
