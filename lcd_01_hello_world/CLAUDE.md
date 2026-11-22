# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A bare-metal (no HAL, no CMSIS, no CubeMX code) learning project for a 2.8" ST7789V 240x320 TFT on SPI1 of an
STM32F103C8T6 "Blue Pill". The goal is to understand how to drive the LCD. The user designs the application in
`Src/main.c` and configures the peripherals themself: the existing drivers are reference code, not a finished
library. Explanations matter as much as code.

## Commands

```bash
./flash.sh                                    # build Debug and flash over ST-Link (one image at 0x08000000)
cmake --preset Debug && cmake --build --preset Debug   # build only -> build/Debug/lcd.elf (Release preset also exists)
python3 tools/gen_font.py                     # regenerate Src/font_data.c + Inc/font.h (needs Pillow + X11 misc fonts)
python3 openocd.py                            # tkinter OpenOCD tutorial + live MCU inspector (needs openocd + ST-Link)
```

- `flash.sh` prepends the toolchain bundles installed by the STM32Cube VS Code extension
  (`~/.local/share/stm32cube/bundles/...`) to `PATH`; a plain `cmake` call needs `arm-none-eabi-gcc`, `cmake` and `ninja`
  on `PATH` itself. Windows equivalent: `flash.ps1`.
- Flashing fails while `openocd` (or `openocd.py` connected) holds the ST-Link; disconnect it first.
- The build must stay free of warnings (`-Wall -Wextra -Wpedantic`). No automated tests; hardware is checked by hand.
- clangd reads `build/Debug/compile_commands.json` (`.clangd`), so build once before relying on code navigation.
- `cmake/flags.cmake`, `cmake/files.cmake` and `project-description.json` are managed by the STM32Cube extension;
  user sources go in `CMakeLists.txt` `target_sources`.

## Architecture

Layers, each using only the one below:

1. `Src/main.c`: the application. Intentionally left empty for the user to write. Do not fill it unless asked.
2. `Src/gfx.c` + `Src/font_data.c`: rectangles and fixed-width text, built only on the four `st7789.h` window calls.
3. `Src/st7789.c`: panel driver. Hardware reset, SWRESET, SLPOUT, COLMOD = 0x55 (RGB565), MADCTL, INVON/OFF,
   NORON, clears the frame memory, DISPON, then reads the ID (RDDID). Also sets up the CS/DC/RESET GPIOs and the
   TIM3_CH3 backlight PWM (20 kHz on PB0).
4. `Src/spi.c`: polling SPI1 master, mode 0, PCLK2 / 4 = 12 MHz, SSM+SSI (PA4 is a GPIO CS, not NSS).
5. `Inc/stm32f103_regs.h`, `Src/clock.c`, `Src/timebase.c`: hand-written register names, 48 MHz clock, 1 ms SysTick.
6. `Src/startup_stm32f103xx.S`, `stm32f103x8_flash.ld`: vector table, startup, memory map (ST-supplied; keep ST terms).
   The image is linked at 0x08000000 (64 KB flash, 20 KB RAM).

Clock: `SystemInit()` runs HSE 8 MHz × 6 = 48 MHz (HSI/2 × 12 fallback), APB1 /2 = 24 MHz, APB2 = 48 MHz. Every
timing (SysTick, SPI divider, TIM3 period) derives from the `*_HZ` constants in `Inc/board.h`; change them together
with `clock.c`. Pin map: `Inc/board.h` and `HARDWARE.md`.

### ST7789 drawing model (non-obvious bits)

- Draw by opening a window (`lcd_set_window` = CASET + RASET + RAMWR, CS stays low), streaming pixels left→right,
  top→bottom, then `lcd_end()` (flush SPI, CS high).
- Commands go in 8-bit SPI frames; pixel data switches SPI to 16-bit frames (DFF) so one RGB565 pixel is one `DR`
  write. DFF may change only while SPE = 0, and D/C must not change until BSY = 0 (`spi1_flush`).
- `spi1_send` waits only for TXE (streaming); `spi1_transfer` clears stale RXNE/OVR before a full-duplex read.
- Panel quirks are `LCD_INVERT` / `LCD_MADCTL` in `board.h` (colour inversion, RGB/BGR, rotation).
- A full-screen fill is 153,600 bytes ≈ 0.1 s at 12 MHz.
- Font glyph codes: ASCII 32–126, then `FONT_DEG` = 0x7F and `FONT_PM` = 0x80 (asserted in `gen_font.py`).

## Code conventions

- C11; every register access goes through the names in `Inc/stm32f103_regs.h`. Add missing registers there.
- Every wait loop has a timeout. Functions return `0` on success, `-1` on error.
- Comments explain *why* and cite the reference manual section or datasheet (RM0008, PM0056, ES096, ST7789V).

## Local-only files

`GITHUB.md` holds a GitHub token from the original MPU6050 project. Never stage or commit it.
