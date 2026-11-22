# 🪟 Windows setup: install, build and flash

Step by step, from a fresh Windows 10 or 11 PC to the firmware running on the Blue Pill. Follow the steps in order: each one needs the one before.

> [!NOTE]
> This guide is built from the project's scripts and from notes that worked on Windows for the project this one grew out of. It has **not yet been run end to end on a Windows PC for this repository**. If a step looks different on yours, please [open an issue](https://github.com/Emersonandrey11/BluePill_ST7789V_240x320_TFT/issues).

## What you need

| Item | What it is for | Required |
| --- | --- | --- |
| Blue Pill (STM32F103C8T6) + 2.8" ST7789V SPI TFT | the hardware | yes |
| ST-Link V2 and 5 jumper wires | flashing and debugging | yes |
| Git | getting the code | yes |
| STM32CubeProgrammer | ST-Link USB driver and a flasher | yes |
| VS Code + STM32Cube extension, **or** CMake + Ninja + Arm GNU Toolchain | compiling | yes |
| Python 3 and OpenOCD | `openocd.py`, the tutorial and live inspector | optional |

> [!IMPORTANT]
> **Work computer?** Installing the ST-Link USB driver needs **administrator rights**. Everything else can be installed per user. If driver installs are blocked, ask IT to install STM32CubeProgrammer, which includes the driver.

---

## 1. Install Git and get the code

Open **PowerShell** (Start menu → type `PowerShell`) and run:

```powershell
winget install Git.Git
```

Close PowerShell and open a **new** window (so it sees Git), then:

```powershell
mkdir C:\dev
cd C:\dev
git clone https://github.com/Emersonandrey11/BluePill_ST7789V_240x320_TFT.git
cd BluePill_ST7789V_240x320_TFT\lcd_01_hello_world
```

> [!WARNING]
> Use a short path **without spaces or accents**, like `C:\dev\...`. Paths such as `OneDrive\Área de Trabalho` break the build tools.

## 2. Install the ST-Link driver (STM32CubeProgrammer)

1. Download **STM32CubeProgrammer** for Windows from [st.com](https://www.st.com/en/development-tools/stm32cubeprog.html) (a free ST account is required).
2. Run the installer and accept the driver installation when it asks.
3. Plug in the ST-Link. Open **Device Manager**: under *Universal Serial Bus devices* it should show **STM32 STLink**, with no warning sign.

If your ST-Link is old, open STM32CubeProgrammer, click **Firmware upgrade** in the ST-Link panel and update it. Newer firmware works better with OpenOCD.

## 3. Install the build tools

Pick **one** option.

### Option A: VS Code + STM32Cube extension (recommended)

This is the same setup the project was made with.

1. Install VS Code:
   ```powershell
   winget install Microsoft.VisualStudioCode
   ```
2. In VS Code, open the Extensions panel (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>X</kbd>), search for **STM32Cube for Visual Studio Code** (publisher: STMicroelectronics) and install it.
3. **File → Open Folder…** and pick **`lcd_01_hello_world`**, not the repository root: the build configuration (`CMakePresets.json`) lives there.
4. The extension downloads its own CMake, Ninja and GNU Tools for STM32, the versions listed in `.settings/bundles.store.json`. Wait until it finishes.

### Option B: standalone tools

```powershell
winget install Kitware.CMake
winget install Ninja-build.Ninja
```

Then install the **Arm GNU Toolchain** (target `arm-none-eabi`, Windows installer `.exe`) from [developer.arm.com](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads). On the installer's last page, tick **"Add path to environment variable"**.

Open a **new** PowerShell window and check that all three answer:

```powershell
cmake --version
ninja --version
arm-none-eabi-gcc --version
```

## 4. Wire the board

Connect the TFT exactly as in the [README wiring table](../README.md#-wiring), and the ST-Link like this:

| ST-Link | Blue Pill | |
| --- | --- | --- |
| SWDIO | DIO (PA13) | debug data |
| SWCLK | CLK (PA14) | debug clock |
| GND | GND | always |
| 3.3V | 3.3 | **only** if the Blue Pill is not powered by its own USB cable |
| RST | R (NRST) | recommended: needed for "connect under reset" in step 6 |

The **BOOT0** jumper must be on **0**.

> [!TIP]
> Swapping SWDIO and SWCLK is the most common wiring mistake. If nothing connects in step 6, check those two first.

## 5. Build

**Option A (VS Code):** in the blue status bar at the bottom, pick the **Debug** preset and click **Build**.

**Option B (PowerShell)**, inside `lcd_01_hello_world`:

```powershell
cmake --preset Debug
cmake --build --preset Debug
```

A good build ends with a memory summary like this (these numbers are for the empty `main.c`; they grow as you write code), and **no warnings**:

```text
Memory region         Used Size  Region Size  %age Used
             RAM:        1984 B        20 KB      9.69%
           FLASH:        1056 B        64 KB      1.61%
```

The firmware is now at `build\Debug\lcd.elf`.

## 6. Flash

Close anything else that uses the ST-Link first (STM32CubeProgrammer, OpenOCD, `openocd.py`). Only one program can own it at a time.

### Way 1: the project script

```powershell
powershell -ExecutionPolicy Bypass -File .\flash.ps1
```

It builds and flashes in one go. `-ExecutionPolicy Bypass` is needed because Windows blocks unsigned scripts by default. If you built in VS Code, add `-NoBuild`.

`flash.ps1` uses the first flasher it finds on `PATH`: `STM32_Programmer_CLI`, then `openocd`, then `st-flash`. To make it use STM32CubeProgrammer, add its `bin` folder to `PATH`. With the default install that is `C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin`.

### Way 2: connect under reset (if Way 1 cannot connect)

Some boards only answer the debugger while held in reset: this project's own board needed it. `flash.ps1` doesn't do that yet, so run the flasher yourself. This needs the **RST** wire from step 4.

```powershell
STM32_Programmer_CLI -c port=SWD mode=UR -w build\Debug\lcd.elf -v -rst
```

`mode=UR` means *under reset*, `-v` verifies, and `-rst` restarts the chip so your program runs.

Or with the **STM32CubeProgrammer window**: set **Mode** to **Under reset** (top right), click **Connect**, open **Erasing & programming** (second icon on the left), browse to `build\Debug\lcd.elf`, tick **Verify programming** and **Run after programming**, then click **Start Programming**.

### Way 3: OpenOCD

If you installed OpenOCD (step 7), this is the configuration that worked on this project's board:

```powershell
openocd -f interface/stlink.cfg -f target/stm32f1x.cfg -c "reset_config srst_only srst_nogate connect_assert_srst" -c "program build/Debug/lcd.elf verify reset exit"
```

## 7. Optional: `openocd.py`, the tutorial and live inspector

1. Install Python. The python.org build includes tkinter, which `openocd.py` needs.
   ```powershell
   winget install Python.Python.3.12
   ```
2. Install OpenOCD: download `xpack-openocd-<version>-win32-x64.zip` (0.12 or newer) from [xpack-dev-tools/openocd-xpack](https://github.com/xpack-dev-tools/openocd-xpack/releases), unzip it to `C:\tools\xpack-openocd`, and add `C:\tools\xpack-openocd\bin` to your user `PATH` (Start → *Edit environment variables for your account* → `Path` → *New*).
3. In a **new** PowerShell window, inside `lcd_01_hello_world`:
   ```powershell
   openocd --version
   python openocd.py
   ```
4. Tick **Connect under reset** if you wired RST, then click **Connect**. The **Chip** tab should show *STM32F10x medium-density* and *Program in flash: yes*.

On Windows, `openocd.py` finds `arm-none-eabi-nm` (used for function names) and the tools behind its **Build** button only through `PATH`. With Option A, build in VS Code instead; the inspector works either way.

## 8. Check that it worked

- STM32CubeProgrammer or OpenOCD reports **verified** at the end of flashing.
- In `openocd.py`, **Chip → Program in flash** reads *yes*, and **Core → Reset + run** starts the program.
- `main.c` is empty on purpose, so the screen stays dark until you write the application. The README's [architecture section](../README.md#-how-the-code-is-organised) says what has to come first (`timebase_init()`).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `git`, `cmake`, `ninja` or `arm-none-eabi-gcc` "not recognized" | Open a **new** PowerShell window after installing; check the tool's folder is in `PATH`. |
| CMake complains about a different source directory or `CMakeCache.txt` | Delete the `build` folder and build again (happens when a `build` folder came from another PC). |
| `flash.ps1` "cannot be loaded because running scripts is disabled" | Start it the way step 6 shows, with `-ExecutionPolicy Bypass`. |
| `flash.ps1` says `arm-none-eabi-gcc not found` with Option A | Build in VS Code, then run `flash.ps1 -NoBuild`. |
| "openocd is running and holds the ST-Link" | Close `openocd.py`, or end `openocd.exe` in Task Manager. |
| `Failed to enter SWD mode`, `unable to connect to the target`, `No STM32 target found` | Check SWDIO/SWCLK (swapped?), GND, BOOT0 = 0. Then use **connect under reset** (step 6, Way 2) with the RST wire. |
| OpenOCD `LIBUSB_ERROR_NOT_SUPPORTED` / `open failed` | ST-Link driver missing (step 2), or another program has the ST-Link open. |
| OpenOCD `UNEXPECTED idcode` | The chip is a clone (CKS32, GD32…). In `openocd.py` tick **Clone chip**, or add `-c "set CPUTAPID 0"` before the target file. |
| Device Manager shows the ST-Link with a warning sign | Reinstall STM32CubeProgrammer as administrator, or the ST-Link driver STSW-LINK009 from st.com. |

## Using Claude Code on Windows

The README section [Starting a new Claude Code session on a fresh clone](../README.md#-starting-a-new-claude-code-session-on-a-fresh-clone) applies as is. Add one line to the first message, *"I am on Windows: use PowerShell commands and `flash.ps1`"*, so the session doesn't suggest the Linux scripts.
