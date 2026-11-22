#!/usr/bin/env bash
# Build the Debug configuration and flash it to the STM32F103C8T6 via ST-Link V2
# (one image at 0x08000000). Uses the toolchain bundles installed by the
# STM32Cube VS Code extension.
#
# Wiring: ST-Link SWDIO, SWCLK, GND to the Blue Pill. If the board is powered
# from its micro-USB port, leave the ST-Link's 3.3 V pin unconnected (two
# supplies on one rail); without USB, connect 3.3 V too so the ST-Link powers it.
set -euo pipefail
cd "$(dirname "$0")"

BUNDLES="$HOME/.local/share/stm32cube/bundles"
export PATH="$BUNDLES/gnu-tools-for-stm32/14.3.1+st.2/bin:$BUNDLES/ninja/1.13.2+st.1/bin:$BUNDLES/cmake/4.4.0+st.1/bin:$PATH"

cmake --preset Debug
cmake --build --preset Debug

ELF=build/Debug/lcd.elf
BIN=build/Debug/lcd.bin
arm-none-eabi-objcopy -O binary "$ELF" "$BIN"
arm-none-eabi-size "$ELF"

# Fail early, with a clear message, when no ST-Link is plugged in
# (USB vendor 0483 = STMicroelectronics: 3748 V2, 374b V2-1, 374e/374f/3753/3754 V3).
if command -v lsusb >/dev/null && ! lsusb | grep -q -E 'ID 0483:(3748|374b|374e|374f|3752|3753|3754)'; then
    cat >&2 <<'MSG'

No ST-Link found on USB (lsusb shows no 0483:37xx device).
  - Plug the ST-Link into the PC (try another port or cable).
  - Wire SWDIO, SWCLK, GND to the Blue Pill (+3.3 V only if the board has no USB power).
The build above is done; run ./flash.sh again once the ST-Link is connected.
MSG
    exit 1
fi

if pgrep -x openocd >/dev/null; then
    echo "openocd is running and holds the ST-Link: stop it first." >&2
    exit 1
fi

if command -v st-flash >/dev/null; then
    if ! st-flash --reset write "$BIN" 0x08000000; then
        cat >&2 <<'MSG'

Flashing failed. Things to try:
  1. Check the 4 ST-Link wires (SWDIO, SWCLK, GND, 3.3V) and that BOOT0 is on 0.
  2. Hold the board's RESET button, run ./flash.sh, release RESET about 1 s later.
  3. Run "st-info --probe" to check whether the chip is seen at all.
MSG
        exit 1
    fi
elif command -v STM32_Programmer_CLI >/dev/null; then
    STM32_Programmer_CLI -c port=SWD -w "$ELF" -v -rst
elif command -v openocd >/dev/null; then
    openocd -f interface/stlink.cfg -f target/stm32f1x.cfg -c "program $ELF verify reset exit"
else
    echo "No flasher found. Install one, e.g.: sudo apt install stlink-tools" >&2
    exit 1
fi

echo "Done: application flashed at 0x08000000."
