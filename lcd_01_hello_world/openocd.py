#!/usr/bin/env python3
"""
openocd.py - OpenOCD Lab: a hands-on OpenOCD tutorial and a live inspector
for the STM32F103C8T6 (Blue Pill) behind an ST-Link.

    python3 openocd.py                       # open the app
    python3 openocd.py --connect             # open and start OpenOCD at once
    python3 openocd.py --attach              # use an OpenOCD that already runs
    python3 openocd.py --elf build/Debug/lcd.elf --port 6666

Standard library only (Debian/Ubuntu: sudo apt install python3-tk openocd).

How it talks to the chip: the app starts OpenOCD (or attaches to a running
one) and sends Tcl commands to its TCL server (port 6666). Each request is a
command followed by the byte 0x1A; the reply ends with 0x1A too. Every
command the app sends on your behalf is printed in the log at the bottom, so
you can repeat it in telnet (port 4444) or put it in a script.

Tabs:
    Tutorial     13 chapters with "Try it" buttons that run on your board
    Chip         instant summary: identity, clocks, reset cause, LCD pins...
    Peripherals  registers decoded bit by bit (RCC, GPIO, SPI1, TIM3, SCB...)
    Core         halt / step / reset, core registers, HardFault analysis
    Memory       hex dump, write, variables from the ELF
    Flash        build, program, verify, erase, option bytes
    Console      type any OpenOCD command
"""

import argparse
import bisect
import glob
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ELF = os.path.join(HERE, "build", "Debug", "lcd.elf")
BOARD_H = os.path.join(HERE, "Inc", "board.h")
BUNDLES = os.path.expanduser("~/.local/share/stm32cube/bundles")
TCL_PORT = 6666
HSE_HZ = 8_000_000          # Blue Pill crystal
HSI_HZ = 8_000_000

# Clock values used by the decoders (updated whenever RCC is read).
STATE = {"sysclk": 48e6, "hclk": 48e6, "pclk1": 24e6, "pclk2": 48e6, "tim_apb1": 48e6, "tim_apb2": 48e6}


# =============================================================================
# Small helpers
# =============================================================================

def bits(value, lsb, width=1):
    return (value >> lsb) & ((1 << width) - 1)


def hex32(v):
    return f"0x{v:08X}"


def mhz(hz):
    if hz >= 1e6:
        return f"{hz / 1e6:.4g} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.4g} kHz"
    return f"{hz:.4g} Hz"


def find_tool(name):
    """A tool from PATH, else from the STM32Cube VS Code extension bundles."""
    path = shutil.which(name)
    if path:
        return path
    hits = sorted(glob.glob(os.path.join(BUNDLES, "*", "*", "bin", name)), reverse=True)
    return hits[0] if hits else None


def bundle_path_env():
    """PATH with the STM32Cube bundles (toolchain, cmake, ninja) in front."""
    dirs = []
    for tool in ("gnu-tools-for-stm32", "ninja", "cmake"):
        hits = sorted(glob.glob(os.path.join(BUNDLES, tool, "*", "bin")), reverse=True)
        if hits:
            dirs.append(hits[0])
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def parse_board_pins(path=BOARD_H):
    """{('A', 5): 'LCD_SCK', ...} from lines like
    '#define LCD_SCK_PIN  5U  /* PA5  -> SCK ...'"""
    labels = {("C", 13): "LED"}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*#define\s+(\w+)_PIN\s+(\d+)U?\s*/\*\s*P([A-G])(\d+)", line)
                if m and int(m.group(2)) == int(m.group(4)):
                    labels[(m.group(3), int(m.group(4)))] = m.group(1)
    except OSError:
        pass
    if len(labels) == 1:   # board.h missing: the wiring of this project
        labels.update({("A", 2): "LCD_RST", ("A", 3): "LCD_DC", ("A", 4): "LCD_CS",
                       ("A", 5): "LCD_SCK", ("A", 6): "LCD_MISO", ("A", 7): "LCD_MOSI",
                       ("B", 0): "LCD_BL"})
    return labels


# =============================================================================
# OpenOCD TCL client
# =============================================================================

class OpenOCDError(Exception):
    pass


class OpenOCD:
    """Starts / attaches to OpenOCD and runs commands over the TCL port."""

    TERM = b"\x1a"

    def __init__(self, log, on_lost):
        self.log = log              # log(text, tag), thread safe
        self.on_lost = on_lost      # called (from any thread) when the link drops
        self.sock = None
        self.proc = None
        self.lock = threading.Lock()
        self.cmdline = ""
        self.have_read_memory = True

    @property
    def connected(self):
        return self.sock is not None

    # ---- process / socket ---------------------------------------------------
    @staticmethod
    def port_in_use(port, host="127.0.0.1"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            return False

    def start(self, args, port):
        self.disconnect()
        if self.port_in_use(port):
            raise OpenOCDError(f"Port {port} is already in use: another OpenOCD is running. "
                               "Use 'Attach' to use it, or stop it (pkill openocd).")
        self.cmdline = " ".join(shlex.quote(a) for a in args)
        self.log("$ " + self.cmdline, "cmd")
        try:
            self.proc = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except FileNotFoundError:
            raise OpenOCDError("openocd not found. Install it: sudo apt install openocd")
        proc = self.proc
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                self.proc = None
                raise OpenOCDError(f"OpenOCD stopped (exit code {proc.returncode}). Read the log below; "
                                   "tutorial chapter 13 lists the usual causes.")
            try:
                self.attach("127.0.0.1", port, quiet=True)
                return
            except OSError:
                time.sleep(0.2)
        self.disconnect()
        raise OpenOCDError("OpenOCD did not open its TCL port within 10 s.")

    def _reader(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            tag = "err" if line.startswith("Error") else ("warn" if line.startswith("Warn") else "ocd")
            self.log(line, tag)
        proc.wait()
        self.log(f"OpenOCD exited (code {proc.returncode})", "warn")
        if self.proc is proc:
            self.proc = None
            self._drop()

    def attach(self, host="127.0.0.1", port=TCL_PORT, quiet=False):
        sock = socket.create_connection((host, port), timeout=2)
        with self.lock:
            self.sock = sock
        if not quiet:
            self.log(f"Attached to OpenOCD TCL server at {host}:{port}", "info")
        try:
            self.cmd("read_memory 0xE0042000 32 1")
            self.have_read_memory = True
        except OpenOCDError as e:
            self.have_read_memory = "invalid command" not in str(e)

    def _drop(self):
        with self.lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
            self.on_lost()

    def disconnect(self):
        self._drop()
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---- commands -----------------------------------------------------------
    def cmd(self, command, timeout=10.0, echo=False):
        """Run one Tcl command; return its result text, raise on error.

        The command is wrapped in catch so success and failure can be told
        apart: the server answers "0|result" or "1|error message"."""
        if echo:
            self.log("» " + command, "cmd")
        wrapped = 'format "%d|%s" [catch {' + command + '} __r] $__r'
        with self.lock:
            if self.sock is None:
                raise OpenOCDError("Not connected. Press Connect (or Attach) at the top first.")
            try:
                self.sock.settimeout(timeout)
                self.sock.sendall(wrapped.encode() + self.TERM)
                buf = bytearray()
                while self.TERM not in buf:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("OpenOCD closed the connection")
                    buf += chunk
            except OSError as e:
                lost = True
                err = e
            else:
                lost = False
        if lost:
            self._drop()
            raise OpenOCDError(f"Connection to OpenOCD lost ({err}).")
        text = buf[:buf.index(self.TERM)].decode(errors="replace")
        rc, _, result = text.partition("|")
        if rc.strip() != "0":
            msg = result.strip() or "command failed (see the log)"
            if echo:
                self.log(msg, "err")
            raise OpenOCDError(msg)
        if echo and result.strip():
            self.log(result.rstrip(), "out")
        return result

    def read(self, addr, width=32, count=1):
        """List of `count` values of `width` bits starting at `addr`."""
        if self.have_read_memory:
            r = self.cmd(f"read_memory 0x{addr:08X} {width} {count}")
        else:   # OpenOCD < 0.12 has no read_memory: parse "0x08000000: 20005000 08000299"
            op = {32: "mdw", 16: "mdh", 8: "mdb"}[width]
            r = self.cmd(f"{op} 0x{addr:08X} {count}")
        try:
            if self.have_read_memory:
                vals = [int(x, 0) for x in r.split()]
            else:
                vals = []
                for line in r.splitlines():
                    if ":" in line:
                        vals += [int(x, 16) for x in line.split(":", 1)[1].split()]
        except ValueError:
            vals = []
        if len(vals) != count:
            raise OpenOCDError(f"unexpected reply to a memory read: {r.strip()!r}")
        return vals

    def read32(self, addr):
        return self.read(addr, 32, 1)[0]

    def write(self, addr, value, width=32, echo=False):
        op = {32: "mww", 16: "mwh", 8: "mwb"}[width]
        self.cmd(f"{op} 0x{addr:08X} 0x{value:X}", echo=echo)

    def state(self):
        return self.cmd("[target current] curstate").strip()

    def core_regs(self):
        """[(name, value or None)] of the core register set (target halted)."""
        out = self.cmd("reg")
        regs, sections = [], 0
        for line in out.splitlines():
            if line.startswith("====="):
                sections += 1
                if sections > 1:
                    break
                continue
            m = re.match(r"\(\d+\)\s+(\S+)\s+\(/\d+\)(?::\s*(0x[0-9a-fA-F]+))?", line.strip())
            if m:
                regs.append((m.group(1), int(m.group(2), 16) if m.group(2) else None))
        return regs

    def reg(self, name):
        m = re.search(r"0x[0-9a-fA-F]+", self.cmd(f"reg {name}"))
        if not m:
            raise OpenOCDError(f"cannot read register {name}")
        return int(m.group(0), 16)


# =============================================================================
# ELF symbols (arm-none-eabi-nm)
# =============================================================================

class Symbols:
    def __init__(self):
        self.path = None
        self.funcs = []       # sorted [(addr, size, name)]
        self.data = []        # [(name, addr, size, type)]
        self._keys = []

    def load(self, path):
        nm = find_tool("arm-none-eabi-nm")
        if not nm:
            raise OpenOCDError("arm-none-eabi-nm not found (install the STM32Cube extension or the ARM toolchain).")
        if not os.path.isfile(path):
            raise OpenOCDError(f"{path} not found: build the project first (Flash tab -> Build).")
        out = subprocess.run([nm, "-S", "-C", "--defined-only", path], capture_output=True, text=True, check=True).stdout
        funcs, data = [], []
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                addr, size, typ, name = int(parts[0], 16), int(parts[1], 16), parts[2], parts[3]
            elif len(parts) == 3:
                addr, size, typ, name = int(parts[0], 16), 0, parts[1], parts[2]
            else:
                continue
            if typ in "TtWw":
                funcs.append((addr & ~1, size, name))
            elif typ in "BbDdRrGgSsVv" and not name.startswith("$"):
                data.append((name, addr, size, typ))
        funcs.sort()
        data.sort(key=lambda d: d[1])
        self.path, self.funcs, self.data = path, funcs, data
        self._keys = [f[0] for f in funcs]

    def lookup(self, addr):
        """'main+0x8' for a code address, '' if unknown."""
        if not self.funcs:
            return ""
        a = addr & ~1
        i = bisect.bisect_right(self._keys, a) - 1
        if i < 0:
            return ""
        start, size, name = self.funcs[i]
        if size and a >= start + size:
            return ""
        off = a - start
        return f"{name}+0x{off:X}" if off else name


# =============================================================================
# STM32F103 knowledge: identification and register decoders
# =============================================================================

DEV_IDS = {
    0x410: "STM32F10x medium-density (F103x8 / F103xB)",
    0x412: "STM32F10x low-density",
    0x414: "STM32F10x high-density",
    0x418: "STM32F105/107 connectivity line",
    0x420: "STM32F100 medium-density value line",
    0x428: "STM32F100 high-density value line",
    0x430: "STM32F10x XL-density",
}
REV_IDS_410 = {0x0000: "A", 0x2000: "B", 0x2001: "Z", 0x2003: "Y, 1, 2 or X"}

CORE_PARTS = {0xC20: "Cortex-M0", 0xC60: "Cortex-M0+", 0xC23: "Cortex-M3", 0xC24: "Cortex-M4", 0xC27: "Cortex-M7"}

IRQ_NAMES = ["WWDG", "PVD", "TAMPER", "RTC", "FLASH", "RCC", "EXTI0", "EXTI1", "EXTI2", "EXTI3", "EXTI4",
             "DMA1_Ch1", "DMA1_Ch2", "DMA1_Ch3", "DMA1_Ch4", "DMA1_Ch5", "DMA1_Ch6", "DMA1_Ch7", "ADC1_2",
             "USB_HP_CAN_TX", "USB_LP_CAN_RX0", "CAN_RX1", "CAN_SCE", "EXTI9_5", "TIM1_BRK", "TIM1_UP",
             "TIM1_TRG_COM", "TIM1_CC", "TIM2", "TIM3", "TIM4", "I2C1_EV", "I2C1_ER", "I2C2_EV", "I2C2_ER",
             "SPI1", "SPI2", "USART1", "USART2", "USART3", "EXTI15_10", "RTCAlarm", "USBWakeup"]

EXCEPTIONS = {0: "none (thread mode)", 1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
              6: "UsageFault", 11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


def exception_name(n):
    if n in EXCEPTIONS:
        return EXCEPTIONS[n]
    if n >= 16:
        irq = n - 16
        return f"IRQ {irq} ({IRQ_NAMES[irq]})" if irq < len(IRQ_NAMES) else f"IRQ {irq}"
    return f"reserved ({n})"


def decode_cpuid(v):
    part = bits(v, 4, 12)
    impl = bits(v, 24, 8)
    name = CORE_PARTS.get(part, f"part 0x{part:03X}")
    vendor = "ARM" if impl == 0x41 else f"implementer 0x{impl:02X}"
    return f"{vendor} {name} r{bits(v, 20, 4)}p{bits(v, 0, 4)}"


def compute_clocks(cr, cfgr, hse=HSE_HZ):
    sws = bits(cfgr, 2, 2)
    if bits(cfgr, 16):
        pll_in = hse / (2 if bits(cfgr, 17) else 1)
        pll_src = f"HSE {mhz(hse)}" + (" /2" if bits(cfgr, 17) else "")
    else:
        pll_in = HSI_HZ / 2
        pll_src = "HSI /2 (4 MHz)"
    mul = min(bits(cfgr, 18, 4) + 2, 16)
    pll = pll_in * mul
    sysclk = {0: HSI_HZ, 1: hse, 2: pll}.get(sws, HSI_HZ)
    src = {0: "HSI (internal 8 MHz RC)", 1: "HSE (crystal)", 2: f"PLL: {pll_src} x{mul}"}.get(sws, "?")
    hpre = bits(cfgr, 4, 4)
    hdiv = 1 if hpre < 8 else [2, 4, 8, 16, 64, 128, 256, 512][hpre - 8]
    hclk = sysclk / hdiv

    def apb(field):
        return 1 if field < 4 else [2, 4, 8, 16][field - 4]
    d1, d2 = apb(bits(cfgr, 8, 3)), apb(bits(cfgr, 11, 3))
    pclk1, pclk2 = hclk / d1, hclk / d2
    return {"sysclk": sysclk, "hclk": hclk, "pclk1": pclk1, "pclk2": pclk2,
            "tim_apb1": pclk1 * (1 if d1 == 1 else 2), "tim_apb2": pclk2 * (1 if d2 == 1 else 2),
            "src": src, "hse_ready": bool(bits(cr, 17)), "pll_ready": bool(bits(cr, 25))}


def pin_mode(n):
    mode, cnf = n & 3, n >> 2
    if mode == 0:
        return ["analog", "input floating", "input pull-up/down (ODR picks)", "reserved"][cnf]
    speed = {1: "10 MHz", 2: "2 MHz", 3: "50 MHz"}[mode]
    return ["output push-pull", "output open-drain", "AF push-pull", "AF open-drain"][cnf] + f", {speed}"


CFSR_BITS = [
    (0, "IACCVIOL", "MemManage: instruction fetch from a forbidden region (e.g. execute from a peripheral address)"),
    (1, "DACCVIOL", "MemManage: data access violation (MMFAR has the address if MMARVALID)"),
    (3, "MUNSTKERR", "MemManage on exception return unstacking"),
    (4, "MSTKERR", "MemManage on exception entry stacking (stack overflow?)"),
    (7, "MMARVALID", "MMFAR holds the faulting address"),
    (8, "IBUSERR", "BusFault on instruction fetch (jump to a bad address)"),
    (9, "PRECISERR", "BusFault: precise data access error; BFAR has the address"),
    (10, "IMPRECISERR", "BusFault: imprecise data error (a buffered write failed; PC is after the culprit)"),
    (11, "UNSTKERR", "BusFault on exception return unstacking"),
    (12, "STKERR", "BusFault on exception entry stacking (stack pointer outside SRAM?)"),
    (15, "BFARVALID", "BFAR holds the faulting address"),
    (16, "UNDEFINSTR", "UsageFault: undefined instruction (corrupted code / bad jump)"),
    (17, "INVSTATE", "UsageFault: invalid state, usually a jump to an even address (Thumb bit 0 missing)"),
    (18, "INVPC", "UsageFault: invalid EXC_RETURN value on exception return"),
    (19, "NOCP", "UsageFault: coprocessor instruction (there is no FPU on Cortex-M3)"),
    (24, "UNALIGNED", "UsageFault: unaligned access with UNALIGN_TRP set"),
    (25, "DIVBYZERO", "UsageFault: divide by zero with DIV_0_TRP set"),
]
HFSR_BITS = [
    (1, "VECTTBL", "bus fault while reading the vector table (VTOR wrong?)"),
    (30, "FORCED", "a configurable fault (MemManage/BusFault/UsageFault) escalated to HardFault: see CFSR"),
    (31, "DEBUGEVT", "debug event (breakpoint without a debugger)"),
]


# ---- register model ---------------------------------------------------------

class Field:
    def __init__(self, name, lsb, width=1, decode=None, note=""):
        self.name, self.lsb, self.width, self.decode, self.note = name, lsb, width, decode, note

    @property
    def bitrange(self):
        return f"[{self.lsb}]" if self.width == 1 else f"[{self.lsb + self.width - 1}:{self.lsb}]"

    def meaning(self, v):
        d = self.decode
        if callable(d):
            text = d(v)
        elif isinstance(d, dict):
            text = d.get(v, "")
        else:
            text = ""
        if self.note:
            text = f"{text} - {self.note}" if text else self.note
        return text


def F(name, lsb, width=1, decode=None, note=""):
    return Field(name, lsb, width, decode, note)


class Reg:
    def __init__(self, name, offset, fields=(), note="", readable=True, only_set=False):
        self.name, self.offset, self.fields, self.note = name, offset, list(fields), note
        self.readable, self.only_set = readable, only_set


class Periph:
    def __init__(self, name, base, desc, regs, clock=None, summary=None, lcd=False):
        self.name, self.base, self.desc, self.regs = name, base, desc, regs
        self.clock, self.summary, self.lcd = clock, summary, lcd


EN = {0: "off", 1: "clock ON"}
YES = {0: "no", 1: "YES"}
OCM = {0: "frozen", 1: "active on match", 2: "inactive on match", 3: "toggle", 4: "force inactive",
       5: "force active", 6: "PWM mode 1", 7: "PWM mode 2"}
APB2ENR = 0x40021018
APB1ENR = 0x4002101C


def _hpre(v):
    return "SYSCLK /1" if v < 8 else f"SYSCLK /{[2, 4, 8, 16, 64, 128, 256, 512][v - 8]}"


def _ppre(v):
    return "HCLK /1" if v < 4 else f"HCLK /{[2, 4, 8, 16][v - 4]}"


def _spi_br(v):
    div = 2 ** (v + 1)
    return f"fPCLK2 /{div} = {mhz(STATE['pclk2'] / div)}"


def rcc_summary(v):
    if not isinstance(v.get("CR"), int) or not isinstance(v.get("CFGR"), int):
        return []
    c = compute_clocks(v["CR"], v["CFGR"])
    STATE.update({k: c[k] for k in STATE})
    return [("SYSCLK source", c["src"]), ("SYSCLK", mhz(c["sysclk"])), ("HCLK (AHB, core)", mhz(c["hclk"])),
            ("PCLK1 (APB1)", mhz(c["pclk1"])), ("PCLK2 (APB2)", mhz(c["pclk2"])),
            ("APB1 timers (TIM2-4)", mhz(c["tim_apb1"])), ("APB2 timers (TIM1)", mhz(c["tim_apb2"]))]


def spi_summary(v):
    cr1, sr = v.get("CR1"), v.get("SR")
    if not isinstance(cr1, int):
        return []
    mode = bits(cr1, 1) * 2 + bits(cr1, 0)
    rows = [("Enabled", "YES" if bits(cr1, 6) else "no (SPE = 0)"),
            ("Role", "master" if bits(cr1, 2) else "slave"),
            ("SPI mode", f"mode {mode} (CPOL {bits(cr1, 1)}, CPHA {bits(cr1, 0)})"
                         + ("  - ST7789 accepts mode 0 or 3" if mode in (0, 3) else "  - ST7789 needs mode 0 or 3!")),
            ("SCK clock", _spi_br(bits(cr1, 3, 3))),
            ("Frame", ("16" if bits(cr1, 11) else "8") + "-bit, " + ("LSB" if bits(cr1, 7) else "MSB") + " first"),
            ("NSS", "software (SSM=1), internal level SSI=" + str(bits(cr1, 8)) if bits(cr1, 9) else "hardware pin")]
    if isinstance(sr, int):
        st = [n for b, n in ((7, "BSY"), (1, "TXE"), (0, "RXNE"), (6, "OVR"), (5, "MODF")) if bits(sr, b)]
        rows.append(("Status", ", ".join(st) or "-"))
        if bits(sr, 5):
            rows.append(("Problem", "MODF: master mode lost - set SSM and SSI when CS is a GPIO"))
    return rows


def tim3_summary(v):
    psc, arr, ccr3, cr1, ccer = (v.get(k) for k in ("PSC", "ARR", "CCR3", "CR1", "CCER"))
    if not all(isinstance(x, int) for x in (psc, arr, ccr3, cr1, ccer)):
        return []
    cnt_hz = STATE["tim_apb1"] / (psc + 1)
    period = arr + 1
    duty = min(ccr3 / period, 1.0) * 100 if period else 0
    return [("Counter", ("running" if bits(cr1, 0) else "stopped") + f" at {mhz(cnt_hz)} (timer clock {mhz(STATE['tim_apb1'])} / {psc + 1})"),
            ("Period", f"{period} counts -> {mhz(cnt_hz / period)}"),
            ("CH3 (PB0, backlight)", f"output {'ON' if bits(ccer, 8) else 'off'}, duty {duty:.1f} % (CCR3 {ccr3} / {period})")]


def systick_summary(v):
    csr, rvr = v.get("CSR"), v.get("RVR")
    if not isinstance(csr, int) or not isinstance(rvr, int):
        return []
    clk = STATE["hclk"] if bits(csr, 2) else STATE["hclk"] / 8
    return [("Running", "YES" if bits(csr, 0) else "no"),
            ("Tick period", f"{(rvr + 1) / clk * 1e3:.4g} ms ({rvr + 1} cycles of {mhz(clk)})"),
            ("Interrupt", "SysTick_Handler on every wrap" if bits(csr, 1) else "off")]


def nvic_summary(v):
    on = []
    for i, key in enumerate(("ISER0", "ISER1")):
        x = v.get(key)
        if isinstance(x, int):
            on += [exception_name(16 + 32 * i + b) for b in range(32) if bits(x, b)]
    return [("Enabled interrupts", ", ".join(on) or "none")]


def scb_summary(v):
    rows = []
    if isinstance(v.get("CPUID"), int):
        rows.append(("Core", decode_cpuid(v["CPUID"])))
    if isinstance(v.get("ICSR"), int):
        rows.append(("Executing", exception_name(bits(v["ICSR"], 0, 9))))
    cfsr, hfsr = v.get("CFSR"), v.get("HFSR")
    if isinstance(cfsr, int) and isinstance(hfsr, int):
        names = [n for b, n, _ in CFSR_BITS if bits(cfsr, b)] + [n for b, n, _ in HFSR_BITS if bits(hfsr, b)]
        rows.append(("Faults", ", ".join(names) or "none"))
    return rows


def gpio_periph(port, base, labels, enable_bit):
    def lab(p):
        lbl = labels.get((port, p))
        return f" ({lbl})" if lbl else ""
    crl = Reg("CRL", 0x00, [F(f"P{port}{p}{lab(p)}", p * 4, 4, pin_mode) for p in range(8)],
              "pins 0-7: 4 bits each = CNF[1:0] MODE[1:0]")
    crh = Reg("CRH", 0x04, [F(f"P{port}{p}{lab(p)}", (p - 8) * 4, 4, pin_mode) for p in range(8, 16)],
              "pins 8-15: 4 bits each")
    idr = Reg("IDR", 0x08, [F(f"P{port}{p}{lab(p)}", p, 1, {0: "low", 1: "high"}) for p in range(16)],
              "pin levels read back (read only)")
    odr = Reg("ODR", 0x0C, [F(f"P{port}{p}{lab(p)}", p, 1, {0: "0", 1: "1"}) for p in range(16)],
              "output latch; for an input with pull: 1 = pull-up, 0 = pull-down")
    bsrr = Reg("BSRR", 0x10, note="write-only: bits 0-15 set, 16-31 reset a pin atomically", readable=False)
    lckr = Reg("LCKR", 0x18, note="configuration lock")
    used = any(k[0] == port and v.startswith("LCD") for k, v in labels.items())
    return Periph(f"GPIO{port}", base, f"General-purpose I/O port {port}. Labels come from Inc/board.h.",
                  [crl, crh, idr, odr, bsrr, lckr], clock=(APB2ENR, enable_bit), lcd=used)


def build_peripherals(labels):
    return [
        Periph("RCC", 0x40021000, "Reset and clock control: clock sources, PLL, bus dividers, peripheral clock enables.", [
            Reg("CR", 0x00, [F("HSION", 0, note="internal 8 MHz RC on"), F("HSIRDY", 1, note="HSI stable"),
                             F("HSEON", 16, note="crystal oscillator on"), F("HSERDY", 17, note="crystal stable"),
                             F("HSEBYP", 18, note="external clock instead of a crystal"),
                             F("CSSON", 19, note="clock security system"),
                             F("PLLON", 24, note="PLL on"), F("PLLRDY", 25, note="PLL locked")], "clock control"),
            Reg("CFGR", 0x04, [F("SW", 0, 2, {0: "HSI", 1: "HSE", 2: "PLL", 3: "not allowed"}, "requested SYSCLK"),
                               F("SWS", 2, 2, {0: "HSI", 1: "HSE", 2: "PLL", 3: "?"}, "SYSCLK actually used"),
                               F("HPRE", 4, 4, _hpre, "AHB prescaler"), F("PPRE1", 8, 3, _ppre, "APB1 (max 36 MHz)"),
                               F("PPRE2", 11, 3, _ppre, "APB2"),
                               F("ADCPRE", 14, 2, {0: "PCLK2 /2", 1: "PCLK2 /4", 2: "PCLK2 /6", 3: "PCLK2 /8"}),
                               F("PLLSRC", 16, 1, {0: "HSI /2", 1: "HSE"}), F("PLLXTPRE", 17, 1, {0: "HSE /1", 1: "HSE /2"}),
                               F("PLLMUL", 18, 4, lambda x: f"x{min(x + 2, 16)}"),
                               F("USBPRE", 22, 1, {0: "PLL /1.5", 1: "PLL /1"}),
                               F("MCO", 24, 3, {0: "off", 4: "SYSCLK", 5: "HSI", 6: "HSE", 7: "PLL /2"}, "clock output on PA8")],
                "clock configuration"),
            Reg("AHBENR", 0x14, [F("DMA1EN", 0, decode=EN), F("SRAMEN", 2, decode=EN), F("FLITFEN", 4, decode=EN),
                                 F("CRCEN", 6, decode=EN)], "AHB peripheral clocks"),
            Reg("APB2ENR", 0x18, [F(n, b, decode=EN) for n, b in (("AFIOEN", 0), ("IOPAEN", 2), ("IOPBEN", 3), ("IOPCEN", 4),
                                                                 ("IOPDEN", 5), ("ADC1EN", 9), ("ADC2EN", 10),
                                                                 ("TIM1EN", 11), ("SPI1EN", 12), ("USART1EN", 14))],
                "APB2 peripheral clocks (a peripheral without clock reads as 0 and ignores writes)"),
            Reg("APB1ENR", 0x1C, [F(n, b, decode=EN) for n, b in (("TIM2EN", 0), ("TIM3EN", 1), ("TIM4EN", 2), ("WWDGEN", 11),
                                                                 ("SPI2EN", 14), ("USART2EN", 17), ("USART3EN", 18),
                                                                 ("I2C1EN", 21), ("I2C2EN", 22), ("USBEN", 23),
                                                                 ("CANEN", 25), ("BKPEN", 27), ("PWREN", 28))],
                "APB1 peripheral clocks"),
            Reg("CSR", 0x24, [F("LSION", 0, note="40 kHz LSI on"), F("LSIRDY", 1),
                              F("PINRSTF", 26, note="reset by the NRST pin"), F("PORRSTF", 27, note="power-on reset"),
                              F("SFTRSTF", 28, note="software reset (SYSRESETREQ, e.g. OpenOCD 'reset')"),
                              F("IWDGRSTF", 29, note="independent watchdog reset"),
                              F("WWDGRSTF", 30, note="window watchdog reset"), F("LPWRRSTF", 31, note="low-power reset")],
                "reset flags add up until firmware writes RMVF (bit 24)"),
        ], summary=rcc_summary),
        Periph("FLASH", 0x40022000, "Flash interface: wait states, programming, read/write protection.", [
            Reg("ACR", 0x00, [F("LATENCY", 0, 3, {0: "0 WS (<= 24 MHz)", 1: "1 WS (<= 48 MHz)", 2: "2 WS (<= 72 MHz)"}),
                              F("HLFCYA", 3, note="half-cycle access"), F("PRFTBE", 4, note="prefetch enable"),
                              F("PRFTBS", 5, note="prefetch active")], "access control"),
            Reg("SR", 0x0C, [F("BSY", 0, note="operation in progress"), F("PGERR", 2, note="programming error"),
                             F("WRPRTERR", 4, note="write to a protected page"), F("EOP", 5, note="end of operation")]),
            Reg("CR", 0x10, [F("PG", 0), F("PER", 1), F("MER", 2), F("OPTPG", 4), F("OPTER", 5), F("STRT", 6),
                             F("LOCK", 7, note="1 = locked (normal)"), F("OPTWRE", 9)]),
            Reg("OBR", 0x1C, [F("OPTERR", 0, note="option byte error"),
                              F("RDPRT", 1, 1, {0: "off", 1: "READ PROTECTION ON"}, "the debugger cannot read flash"),
                              F("WDG_SW", 2, 1, {0: "hardware watchdog (always on)", 1: "software watchdog"}),
                              F("DATA0", 10, 8), F("DATA1", 18, 8)], "option bytes as loaded at reset"),
            Reg("WRPR", 0x20, note="write protection: a 0 bit protects 4 KB (0xFFFFFFFF = none)"),
        ]),
        gpio_periph("A", 0x40010800, labels, 2),
        gpio_periph("B", 0x40010C00, labels, 3),
        gpio_periph("C", 0x40011000, labels, 4),
        Periph("AFIO", 0x40010000, "Alternate-function remap: which pins a peripheral uses.", [
            Reg("MAPR", 0x04, [F("SPI1_REMAP", 0, 1, {0: "no remap: NSS PA4, SCK PA5, MISO PA6, MOSI PA7",
                                                      1: "remap: NSS PA15, SCK PB3, MISO PB4, MOSI PB5"}),
                               F("I2C1_REMAP", 1), F("USART1_REMAP", 2), F("USART2_REMAP", 3),
                               F("USART3_REMAP", 4, 2), F("TIM1_REMAP", 6, 2), F("TIM2_REMAP", 8, 2),
                               F("TIM3_REMAP", 10, 2, {0: "no remap: CH1 PA6, CH2 PA7, CH3 PB0, CH4 PB1",
                                                       2: "partial: CH1 PB4, CH2 PB5, CH3 PB0, CH4 PB1",
                                                       3: "full: PC6-PC9"}),
                               F("TIM4_REMAP", 12), F("PD01_REMAP", 15),
                               F("SWJ_CFG", 24, 3, note="write-only, always reads 0 (100 = SWD off!)")],
                "remap and debug port configuration"),
        ], clock=(APB2ENR, 0)),
        Periph("SPI1", 0x40013000, "SPI1 - the LCD bus (SCK PA5, MISO PA6, MOSI PA7).", [
            Reg("CR1", 0x00, [F("CPHA", 0, 1, {0: "sample on 1st edge", 1: "sample on 2nd edge"}),
                              F("CPOL", 1, 1, {0: "SCK idles low", 1: "SCK idles high"}),
                              F("MSTR", 2, 1, {0: "slave", 1: "master"}), F("BR", 3, 3, _spi_br),
                              F("SPE", 6, 1, {0: "disabled", 1: "ENABLED"}),
                              F("LSBFIRST", 7, 1, {0: "MSB first", 1: "LSB first"}),
                              F("SSI", 8, note="internal NSS level when SSM = 1 (must be 1 for master)"),
                              F("SSM", 9, 1, {0: "NSS from the pin", 1: "software NSS"}),
                              F("RXONLY", 10), F("DFF", 11, 1, {0: "8-bit frames", 1: "16-bit frames"}),
                              F("CRCNEXT", 12), F("CRCEN", 13), F("BIDIOE", 14),
                              F("BIDIMODE", 15, 1, {0: "2-line", 1: "1-line bidirectional"})], "control 1"),
            Reg("CR2", 0x04, [F("RXDMAEN", 0), F("TXDMAEN", 1), F("SSOE", 2, note="drive NSS pin as output"),
                              F("ERRIE", 5), F("RXNEIE", 6), F("TXEIE", 7)], "control 2 (DMA, interrupts)"),
            Reg("SR", 0x08, [F("RXNE", 0, note="a received byte waits in DR"), F("TXE", 1, note="transmit buffer empty"),
                             F("CHSIDE", 2), F("UDR", 3), F("CRCERR", 4),
                             F("MODF", 5, note="mode fault: NSS went low, master mode lost"),
                             F("OVR", 6, note="overrun: received data not read (normal when only sending)"),
                             F("BSY", 7, note="busy shifting a frame")], "status"),
            Reg("DR", 0x0C, note="not read: reading DR clears RXNE and steals data from your program", readable=False),
        ], clock=(APB2ENR, 12), summary=spi_summary, lcd=True),
        Periph("TIM3", 0x40000400, "TIM3 - CH3 on PB0 drives the LCD backlight with PWM.", [
            Reg("CR1", 0x00, [F("CEN", 0, 1, {0: "stopped", 1: "COUNTING"}), F("UDIS", 1), F("URS", 2),
                              F("OPM", 3, note="one-pulse mode"), F("DIR", 4, 1, {0: "up", 1: "down"}),
                              F("CMS", 5, 2), F("ARPE", 7, note="ARR preload"), F("CKD", 8, 2)]),
            Reg("SR", 0x10, [F("UIF", 0, note="update (overflow)"), F("CC1IF", 1), F("CC2IF", 2), F("CC3IF", 3), F("CC4IF", 4)]),
            Reg("CCMR2", 0x1C, [F("CC3S", 0, 2, {0: "output", 1: "input TI3", 2: "input TI4", 3: "input TRC"}),
                                F("OC3PE", 3, note="CCR3 preload"), F("OC3M", 4, 3, OCM),
                                F("CC4S", 8, 2), F("OC4PE", 11), F("OC4M", 12, 3, OCM)], "channels 3 and 4 mode"),
            Reg("CCER", 0x20, [F("CC1E", 0), F("CC2E", 4), F("CC3E", 8, note="CH3 drives its pin (PB0)"),
                               F("CC3P", 9, 1, {0: "active high", 1: "active low"}), F("CC4E", 12)], "channel enables"),
            Reg("CNT", 0x24, note="counter"),
            Reg("PSC", 0x28, note="prescaler: counter clock = timer clock / (PSC + 1)"),
            Reg("ARR", 0x2C, note="auto-reload: period = ARR + 1 counts"),
            Reg("CCR3", 0x3C, note="CH3 compare = backlight duty"),
        ], clock=(APB1ENR, 1), summary=tim3_summary, lcd=True),
        Periph("SysTick", 0xE000E010, "Core 24-bit timer; timebase.c uses it for the 1 ms tick (millis, delay_ms).", [
            Reg("CSR", 0x00, [F("ENABLE", 0), F("TICKINT", 1, note="interrupt on wrap"),
                              F("CLKSOURCE", 2, 1, {0: "HCLK / 8", 1: "HCLK"}),
                              F("COUNTFLAG", 16, note="wrapped since last read (reading clears it)")]),
            Reg("RVR", 0x04, note="reload value"), Reg("CVR", 0x08, note="current value (counts down)"),
            Reg("CALIB", 0x0C, note="calibration"),
        ], summary=systick_summary),
        Periph("NVIC", 0xE000E100, "Interrupt controller: which interrupts are enabled / pending / active.", [
            Reg("ISER0", 0x000, [F(f"{i} {IRQ_NAMES[i]}", i) for i in range(32)], "enabled IRQ 0-31", only_set=True),
            Reg("ISER1", 0x004, [F(f"{i + 32} {IRQ_NAMES[i + 32]}", i) for i in range(len(IRQ_NAMES) - 32)],
                "enabled IRQ 32-42", only_set=True),
            Reg("ISPR0", 0x100, [F(f"{i} {IRQ_NAMES[i]}", i) for i in range(32)], "pending IRQ 0-31", only_set=True),
            Reg("ISPR1", 0x104, [F(f"{i + 32} {IRQ_NAMES[i + 32]}", i) for i in range(len(IRQ_NAMES) - 32)],
                "pending IRQ 32-42", only_set=True),
            Reg("IABR0", 0x200, [F(f"{i} {IRQ_NAMES[i]}", i) for i in range(32)], "active IRQ 0-31", only_set=True),
        ], summary=nvic_summary),
        Periph("SCB", 0xE000ED00, "System control block: core ID, active exception, vector table, fault status.", [
            Reg("CPUID", 0x00, [F("REVISION", 0, 4), F("PARTNO", 4, 12, CORE_PARTS), F("VARIANT", 20, 4),
                                F("IMPLEMENTER", 24, 8, {0x41: "ARM"})]),
            Reg("ICSR", 0x04, [F("VECTACTIVE", 0, 9, exception_name, "running now"), F("RETTOBASE", 11),
                               F("VECTPENDING", 12, 10, exception_name, "next to run"), F("ISRPENDING", 22),
                               F("PENDSTSET", 26), F("PENDSVSET", 28)], "interrupt control and state"),
            Reg("VTOR", 0x08, note="vector table address"),
            Reg("AIRCR", 0x0C, [F("PRIGROUP", 8, 3), F("ENDIANNESS", 15)]),
            Reg("SCR", 0x10, [F("SLEEPONEXIT", 1), F("SLEEPDEEP", 2)]),
            Reg("CCR", 0x14, [F("UNALIGN_TRP", 3, note="fault on unaligned access"),
                              F("DIV_0_TRP", 4, note="fault on divide by zero"), F("STKALIGN", 9)]),
            Reg("SHCSR", 0x24, [F("MEMFAULTACT", 0), F("BUSFAULTACT", 1), F("USGFAULTACT", 3), F("SVCALLACT", 7),
                                F("PENDSVACT", 10), F("SYSTICKACT", 11), F("MEMFAULTENA", 16),
                                F("BUSFAULTENA", 17), F("USGFAULTENA", 18)], "system handler state"),
            Reg("CFSR", 0x28, [F(n, b, note=t) for b, n, t in CFSR_BITS], "configurable fault status", only_set=True),
            Reg("HFSR", 0x2C, [F(n, b, note=t) for b, n, t in HFSR_BITS], "HardFault status", only_set=True),
            Reg("MMFAR", 0x34, note="MemManage fault address (valid if MMARVALID)"),
            Reg("BFAR", 0x38, note="BusFault address (valid if BFARVALID)"),
        ], summary=scb_summary),
        Periph("DBGMCU", 0xE0042000, "Debug support: chip ID and what stops while the core is halted.", [
            Reg("IDCODE", 0x00, [F("DEV_ID", 0, 12, DEV_IDS), F("REV_ID", 16, 16, lambda x: f"0x{x:04X}")]),
            Reg("CR", 0x04, [F("DBG_SLEEP", 0, note="keep debug alive in sleep"), F("DBG_STOP", 1), F("DBG_STANDBY", 2),
                             F("TRACE_IOEN", 5), F("DBG_IWDG_STOP", 8, note="freeze the watchdog when halted"),
                             F("DBG_WWDG_STOP", 9), F("DBG_TIM1_STOP", 10), F("DBG_TIM2_STOP", 11),
                             F("DBG_TIM3_STOP", 12, note="freeze TIM3 (backlight PWM) when halted"),
                             F("DBG_TIM4_STOP", 13)]),
        ]),
    ]


# =============================================================================
# Readers that run in worker threads
# =============================================================================

def safe(fn, default=None):
    try:
        return fn()
    except OpenOCDError:
        return default


def collect_chip(ocd, labels, syms):
    """Everything for the Chip tab: ([(section, [(name, value, detail)])], summary, clocks)."""
    S = []
    version = safe(lambda: ocd.cmd("version").strip(), "?")
    target = safe(lambda: ocd.cmd("target current").strip(), "?")
    state = safe(ocd.state, "?")
    speed = safe(lambda: ocd.cmd("adapter speed").strip(), "?")
    S.append(("Connection", [("OpenOCD", version, ""), ("Target", target, ""),
                             ("Adapter speed", speed, "SWD clock; lower it if reads fail on long wires"),
                             ("CPU state", state, "halted = stopped by the debugger")]))

    rows = []
    idcode = safe(lambda: ocd.read32(0xE0042000))
    cpuid = safe(lambda: ocd.read32(0xE000ED00))
    flash_kb = safe(lambda: ocd.read(0x1FFFF7E0, 16, 1)[0])
    uid = safe(lambda: ocd.read(0x1FFFF7E8, 32, 3))
    dev = "?"
    if idcode is not None:
        dev_id, rev_id = bits(idcode, 0, 12), bits(idcode, 16, 16)
        dev = DEV_IDS.get(dev_id, f"unknown device 0x{dev_id:03X}")
        rev = REV_IDS_410.get(rev_id, f"0x{rev_id:04X}") if dev_id == 0x410 else f"0x{rev_id:04X}"
        rows += [("Device", dev, f"DBGMCU_IDCODE {hex32(idcode)}: DEV_ID 0x{dev_id:03X}"),
                 ("Revision", rev, f"REV_ID 0x{rev_id:04X}")]
    if cpuid is not None:
        hint = ""
        if bits(cpuid, 4, 12) == 0xC23:
            hint = ("genuine STM32F103 cores are r1p1" if (bits(cpuid, 20, 4), bits(cpuid, 0, 4)) == (1, 1)
                    else "not r1p1: probably a clone (CKS32 / GD32 / APM32) - fine, but timings may differ")
        rows.append(("Core", decode_cpuid(cpuid), f"CPUID {hex32(cpuid)}; {hint}"))
    if flash_kb is not None:
        rows.append(("Flash size", f"{flash_kb} KB", "register at 0x1FFFF7E0 (many C8 chips really have 128 KB)"))
    rows.append(("SRAM", "20 KB", "0x20000000 - 0x20004FFF"))
    if uid:
        rows.append(("Unique ID", " ".join(f"{w:08X}" for w in uid), "96-bit, factory programmed at 0x1FFFF7E8"))
    S.append(("Identity", rows))

    rows = []
    dhcsr = safe(lambda: ocd.read32(0xE000EDF0))
    icsr = safe(lambda: ocd.read32(0xE000ED04))
    vtor = safe(lambda: ocd.read32(0xE000ED08))
    if dhcsr is not None:
        flags = [n for b, n in ((17, "halted"), (18, "sleeping (WFI)"), (19, "LOCKUP"), (25, "was reset")) if bits(dhcsr, b)]
        rows.append(("Debug status", ", ".join(flags) or "running", f"DHCSR {hex32(dhcsr)}"))
    if icsr is not None:
        rows.append(("Executing", exception_name(bits(icsr, 0, 9)), "ICSR.VECTACTIVE"))
    if state == "halted":
        pc = safe(lambda: ocd.reg("pc"))
        sp = safe(lambda: ocd.reg("sp"))
        if pc is not None:
            rows.append(("PC", hex32(pc), syms.lookup(pc)))
        if sp is not None:
            rows.append(("SP", hex32(sp), f"{0x20005000 - sp} bytes of stack in use" if 0x20000000 <= sp <= 0x20005000 else ""))
    if vtor is not None:
        rows.append(("Vector table (VTOR)", hex32(vtor), ""))
    vec = safe(lambda: ocd.read(0x08000000, 32, 2))
    if vec:
        if vec == [0xFFFFFFFF, 0xFFFFFFFF]:
            rows.append(("Program in flash", "NONE (flash erased)", "flash it from the Flash tab"))
        else:
            ok = 0x20000000 < vec[0] <= 0x20005000 and vec[1] & 1 and 0x08000000 <= vec[1] < 0x08020000
            rows.append(("Program in flash", "yes" if ok else "suspicious vector table",
                         f"initial SP {hex32(vec[0])}, Reset_Handler {hex32(vec[1])} {syms.lookup(vec[1])}"))
    cfsr = safe(lambda: ocd.read32(0xE000ED28))
    hfsr = safe(lambda: ocd.read32(0xE000ED2C))
    if cfsr is not None and hfsr is not None:
        names = [n for b, n, _ in CFSR_BITS if bits(cfsr, b)] + [n for b, n, _ in HFSR_BITS if bits(hfsr, b)]
        rows.append(("Fault status", ", ".join(names) or "no faults", f"CFSR {hex32(cfsr)}  HFSR {hex32(hfsr)}"))
    S.append(("Core", rows))

    rows, clocks = [], None
    cr = safe(lambda: ocd.read32(0x40021000))
    cfgr = safe(lambda: ocd.read32(0x40021004))
    if cr is not None and cfgr is not None:
        clocks = compute_clocks(cr, cfgr)
        rows += [("SYSCLK source", clocks["src"], f"RCC_CFGR {hex32(cfgr)}"),
                 ("Crystal (HSE)", "running" if clocks["hse_ready"] else "off / not started", f"RCC_CR {hex32(cr)}"),
                 ("SYSCLK / HCLK", f"{mhz(clocks['sysclk'])} / {mhz(clocks['hclk'])}", "core and AHB bus"),
                 ("PCLK1 / PCLK2", f"{mhz(clocks['pclk1'])} / {mhz(clocks['pclk2'])}", "APB1 / APB2 (SPI1 on APB2)"),
                 ("Timer clocks", f"APB1 {mhz(clocks['tim_apb1'])}, APB2 {mhz(clocks['tim_apb2'])}", "x2 when the APB divider != 1")]
        acr = safe(lambda: ocd.read32(0x40022000))
        if acr is not None:
            ws = bits(acr, 0, 3)
            need = 0 if clocks["sysclk"] <= 24e6 else (1 if clocks["sysclk"] <= 48e6 else 2)
            rows.append(("Flash wait states", f"{ws}" + ("" if ws >= need else f"  TOO FEW (need {need})"),
                         f"FLASH_ACR {hex32(acr)}"))
    csr = safe(lambda: ocd.read32(0xE000E010))
    rvr = safe(lambda: ocd.read32(0xE000E014))
    if csr is not None and rvr is not None and clocks:
        clk = clocks["hclk"] if bits(csr, 2) else clocks["hclk"] / 8
        rows.append(("SysTick", f"{(rvr + 1) / clk * 1e3:.4g} ms period" if bits(csr, 0) else "off",
                     f"reload {rvr}, {'interrupt on' if bits(csr, 1) else 'no interrupt'}"))
    S.append(("Clocks", rows))

    rows = []
    rcsr = safe(lambda: ocd.read32(0x40021024))
    if rcsr is not None:
        causes = [n for b, n in ((26, "NRST pin"), (27, "power-on"), (28, "software"), (29, "independent watchdog"),
                                 (30, "window watchdog"), (31, "low power")) if bits(rcsr, b)]
        rows.append(("Reset flags", ", ".join(causes) or "none", "RCC_CSR: flags add up until firmware clears them (RMVF)"))
    obr = safe(lambda: ocd.read32(0x4002201C))
    wrpr = safe(lambda: ocd.read32(0x40022020))
    if obr is not None:
        rows.append(("Read protection", "ON" if bits(obr, 1) else "off", "FLASH_OBR.RDPRT"))
    if wrpr is not None:
        rows.append(("Write protection", "none" if wrpr == 0xFFFFFFFF else f"WRPR {hex32(wrpr)}", "a 0 bit protects 4 KB"))
    S.append(("Reset and protection", rows))

    rows = []
    ahb, apb2, apb1 = (safe(lambda a=a: ocd.read32(a)) for a in (0x40021014, APB2ENR, APB1ENR))
    if None not in (ahb, apb2, apb1):
        def on(val, table):
            return [n for b, n in table if bits(val, b)]
        rows.append(("AHB", ", ".join(on(ahb, ((0, "DMA1"), (2, "SRAM"), (4, "FLITF"), (6, "CRC")))) or "-", ""))
        rows.append(("APB2", ", ".join(on(apb2, ((0, "AFIO"), (2, "GPIOA"), (3, "GPIOB"), (4, "GPIOC"), (5, "GPIOD"),
                                                  (9, "ADC1"), (10, "ADC2"), (11, "TIM1"), (12, "SPI1"), (14, "USART1")))) or "-", ""))
        rows.append(("APB1", ", ".join(on(apb1, ((0, "TIM2"), (1, "TIM3"), (2, "TIM4"), (11, "WWDG"), (14, "SPI2"),
                                                  (17, "USART2"), (18, "USART3"), (21, "I2C1"), (22, "I2C2"),
                                                  (23, "USB"), (27, "BKP"), (28, "PWR")))) or "-", ""))
    S.append(("Peripheral clocks on", rows))

    rows = []
    ports = {"A": 0x40010800, "B": 0x40010C00, "C": 0x40011000}
    cache = {}
    for (port, pin), name in sorted(labels.items(), key=lambda kv: (not kv[1].startswith("LCD"), kv[0])):
        base = ports.get(port)
        if base is None:
            continue
        if port not in cache:
            cache[port] = safe(lambda b=base: ocd.read(b, 32, 3))    # CRL, CRH, IDR
        regs = cache[port]
        if not regs:
            continue
        cfg = bits(regs[0] if pin < 8 else regs[1], (pin % 8) * 4, 4)
        rows.append((f"{name}", f"P{port}{pin}: {pin_mode(cfg)}", f"level {'high' if bits(regs[2], pin) else 'low'}"))
    if apb2 is not None and bits(apb2, 12):
        cr1 = safe(lambda: ocd.read32(0x40013000))
        if cr1 is not None:
            mode = bits(cr1, 1) * 2 + bits(cr1, 0)
            rows.append(("SPI1", ("enabled" if bits(cr1, 6) else "DISABLED") + f", {'master' if bits(cr1, 2) else 'slave'}, "
                         f"mode {mode}, {'16' if bits(cr1, 11) else '8'}-bit",
                         _spi_br(bits(cr1, 3, 3)) if clocks is None else
                         f"SCK {mhz(clocks['pclk2'] / 2 ** (bits(cr1, 3, 3) + 1))}"))
    elif apb2 is not None:
        rows.append(("SPI1", "clock off (RCC_APB2ENR.SPI1EN = 0)", "not configured yet"))
    S.append(("LCD wiring (Inc/board.h)", rows))

    summary = f"{dev.split(' (')[0]}  |  {decode_cpuid(cpuid) if cpuid is not None else '?'}  |  " \
              f"{flash_kb if flash_kb is not None else '?'} KB  |  {state}" + \
              (f"  |  SYSCLK {mhz(clocks['sysclk'])}" if clocks else "")
    return S, summary, clocks


def read_periph(ocd, p):
    clk = None
    if p.clock:
        clk = bool(bits(ocd.read32(p.clock[0]), p.clock[1]))
    vals = {}
    for r in p.regs:
        if not r.readable:
            vals[r.name] = None
            continue
        try:
            vals[r.name] = ocd.read32(p.base + r.offset)
        except OpenOCDError as e:
            vals[r.name] = e
    if p.name in ("SPI1", "TIM3", "SysTick"):     # their decoders need the clock tree
        try:
            vals["__clocks"] = compute_clocks(ocd.read32(0x40021000), ocd.read32(0x40021004))
        except OpenOCDError:
            pass
    return clk, vals


def analyse_fault(ocd, syms):
    lines = []
    cfsr, hfsr = ocd.read32(0xE000ED28), ocd.read32(0xE000ED2C)
    mmfar, bfar = ocd.read32(0xE000ED34), ocd.read32(0xE000ED38)
    icsr = ocd.read32(0xE000ED04)
    lines.append(f"Executing now : {exception_name(bits(icsr, 0, 9))}   (ICSR {hex32(icsr)})")
    lines.append(f"CFSR {hex32(cfsr)}   HFSR {hex32(hfsr)}")
    found = False
    for b, n, t in HFSR_BITS:
        if bits(hfsr, b):
            lines.append(f"  HFSR.{n:<11} {t}")
            found = True
    for b, n, t in CFSR_BITS:
        if bits(cfsr, b):
            lines.append(f"  CFSR.{n:<11} {t}")
            found = True
    if bits(cfsr, 7):
        lines.append(f"  MMFAR = {hex32(mmfar)}  <- the address that was accessed")
    if bits(cfsr, 15):
        lines.append(f"  BFAR  = {hex32(bfar)}  <- the address that was accessed")
    if not found:
        lines.append("  No fault bits set: the program has not faulted since the last reset.")
    state = ocd.state()
    if state != "halted":
        lines.append("\nHalt the core to see where it is and to unwind the exception stack frame.")
        return "\n".join(lines)
    pc, lr = ocd.reg("pc"), ocd.reg("lr")
    lines.append(f"\nPC {hex32(pc)} {syms.lookup(pc)}")
    lines.append(f"LR {hex32(lr)} {syms.lookup(lr) if lr < 0xF0000000 else '(EXC_RETURN: inside an exception handler)'}")
    if (lr & 0xFFFFFFF0) == 0xFFFFFFF0:
        use_psp = bool(lr & 4)
        sp = ocd.reg("psp" if use_psp else "msp")
        frame = ocd.read(sp, 32, 8)
        names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xPSR"]
        lines.append(f"\nException stack frame on {'PSP' if use_psp else 'MSP'} at {hex32(sp)} "
                     "(pushed by the hardware when the exception started):")
        for n, v in zip(names, frame):
            sym = syms.lookup(v) if n in ("lr", "pc") else ""
            lines.append(f"  {n:<5} {hex32(v)} {sym}")
        lines.append(f"\n=> The interrupted code was at {hex32(frame[6])} {syms.lookup(frame[6])}.")
        lines.append("   Look it up:  arm-none-eabi-addr2line -e build/Debug/lcd.elf " + hex32(frame[6]))
    return "\n".join(lines)


# =============================================================================
# Tutorial
# =============================================================================
# Markup: "## " sub-heading, 4-space indent = code, "! " = warning.

TUTORIAL = [
    {"title": "1. What OpenOCD is", "text": """\
OpenOCD (Open On-Chip Debugger) is a program on your PC that talks to the debug
hardware built into the microcontroller. It sits between your tools and the chip:

    arm-none-eabi-gdb / VS Code --+
    telnet (you, typing) ---------+--> OpenOCD --USB--> ST-Link --SWD--> STM32F103
    this app (TCL port) ----------+                      (SWDIO, SWCLK, GND)

## The pieces
Adapter (interface): the USB probe, here an ST-Link V2. Config file: interface/stlink.cfg
Transport: the wire protocol. The Blue Pill uses SWD (2 signal wires) instead of JTAG.
Target: the chip. target/stm32f1x.cfg describes the Cortex-M3 core, its debug port
    and the flash driver for STM32F1 flash.
Servers: once connected, OpenOCD listens on three TCP ports (chapter 3).

## Why it works while your program runs
Every Cortex-M3 has a Debug Access Port (DAP). Through it the debugger reads and
writes any address on the bus - RAM, flash, peripheral registers - without stopping
the CPU. That is how the Chip and Peripherals tabs show live values, and how you
can poke a register by hand and watch the hardware react.

Press Connect at the top, then try the buttons below.""",
     "try": [("OpenOCD version", "version"), ("List targets and their state", "targets"),
             ("Current transport", "transport select")]},

    {"title": "2. Starting OpenOCD", "text": """\
From the project folder:

    openocd -f interface/stlink.cfg -f target/stm32f1x.cfg

What the output means:

    Info : clock speed 1000 kHz                       SWD clock (adapter speed)
    Info : STLINK V2J37S7 (API v2) VID:PID 0483:3748  the probe was found
    Info : Target voltage: 3.24                       the probe sees the board's 3.3 V
    Info : [stm32f1x.cpu] Cortex-M3 r1p1 processor detected
    Info : [stm32f1x.cpu] target has 6 breakpoints, 4 watchpoints
    Info : starting gdb server for stm32f1x.cpu on 3333
    Info : Listening on port 4444 for telnet connections

The Connect button runs exactly this (look at the first line of the log below).

## -f and -c
-f FILE runs a config file, -c "COMMAND" runs one command. They run in order; then
OpenOCD runs `init` and connects to the chip. Config files are searched in
/usr/share/openocd/scripts/ (Linux packages) - have a look inside, they are plain Tcl.

    openocd -f interface/stlink.cfg -f target/stm32f1x.cfg -c "adapter speed 4000"

## Blue Pill clones
Many boards carry a CKS32, CS32 or GD32 instead of a genuine STM32. Their SWD ID differs:

    Warn : UNEXPECTED idcode: 0x2ba01477
    Error: expected 1 of 1: 0x1ba01477

Fix: set CPUTAPID 0 before the target file (the "Clone chip" box at the top):

    openocd -f interface/stlink.cfg -c "set CPUTAPID 0" -f target/stm32f1x.cfg

## A project config file
Put the lines in openocd.cfg in the project folder; plain `openocd` then loads it:

    source [find interface/stlink.cfg]
    source [find target/stm32f1x.cfg]
    adapter speed 4000""",
     "try": [("Show adapter speed", "adapter speed"), ("Show the adapter driver", "adapter name")]},

    {"title": "3. The three servers", "text": """\
After `init`, OpenOCD listens on:

    3333  GDB server     for arm-none-eabi-gdb and VS Code (chapter 9)
    4444  telnet         you type commands:   telnet localhost 4444   (or: nc localhost 4444)
    6666  TCL server     for programs - this app uses it

Every command in this tutorial also works in telnet. Try it in a terminal while this
app is connected: telnet localhost 4444, then type  mdw 0xE0042000

## The TCL protocol
Send a command followed by the byte 0x1A, read until 0x1A. That is all:

    import socket
    s = socket.create_connection(("localhost", 6666))
    s.sendall(b"read_memory 0xE0042000 32 1\\x1a")
    print(s.recv(4096).rstrip(b"\\x1a").decode())      # -> 0x20036410

## Everything is Tcl
The command language is (Jim) Tcl. Variables, [command substitution] and expr work:

    set id [read_memory 0xE0042000 32 1]
    format 0x%03x [expr {$id & 0xfff}]                 # -> 0x410 = F103 medium density

! Only one program can own the ST-Link: ./flash.sh and st-flash fail while OpenOCD runs.
! Use the Flash tab here, or Disconnect first.""",
     "try": [("Chip ID with Tcl", "format 0x%03x [expr {[read_memory 0xE0042000 32 1] & 0xfff}]"),
             ("Tcl arithmetic", "expr {48000000 / 4}")]},

    {"title": "4. Halt, resume, step, reset", "text": """\
    halt               stop the CPU (it stays stopped until resume)
    resume             continue where it stopped
    resume 0x08000298  continue at an address
    step               execute one instruction
    reset run          reset the chip and let it run
    reset halt         reset and stop at the very first instruction (Reset_Handler)
    reset init         reset halt + the target's init script
    [target current] curstate      -> running / halted / reset
    targets                        all targets with their state

When the CPU is halted, peripherals keep going: TIM3 keeps making the backlight
PWM, a SPI transfer in progress finishes. DBGMCU_CR (0xE0042004) can freeze timers
and watchdogs while halted (DBG_TIM3_STOP, DBG_IWDG_STOP).

Halt, then step a few times and watch the PC change (chapter 7 shows registers).
With an empty main loop the PC jumps between two addresses: the loop itself.""",
     "try": [("State", "[target current] curstate"), ("Halt", "halt"), ("Where am I? (PC)", "reg pc"),
             ("Step one instruction", "step"), ("Resume", "resume"), ("Reset and run", "reset run"),
             ("Reset and halt", "reset halt")]},

    {"title": "5. Reading memory", "text": """\
The STM32F103 memory map - everything is an address:

    0x0800 0000  Flash, 64 KB         your program; the vector table comes first
    0x1FFF F000  System memory        ST's built-in serial bootloader (2 KB)
    0x1FFF F7E0  Flash size           16-bit, in KB
    0x1FFF F7E8  Unique ID            96 bits
    0x1FFF F800  Option bytes
    0x2000 0000  SRAM, 20 KB          variables and stack (stack top 0x2000 5000)
    0x4000 0000  APB1 peripherals     TIM3 0x4000 0400 ...
    0x4001 0000  APB2 peripherals     AFIO, GPIOA 0x4001 0800, GPIOB 0x4001 0C00, SPI1 0x4001 3000
    0x4002 1000  RCC                  clock control
    0x4002 2000  Flash interface
    0xE000 E000  Cortex-M3 core       SysTick 0xE000 E010, NVIC 0xE000 E100, SCB 0xE000 ED00
    0xE004 2000  DBGMCU               chip ID code

## Commands
    mdw ADDR [COUNT]               memory display words (32-bit)
    mdh / mdb ADDR [COUNT]         half-words / bytes
    read_memory ADDR WIDTH COUNT   returns a Tcl list - best for scripts

The first two words of flash are the vector table: the initial stack pointer and the
address of Reset_Handler. That address is odd: bit 0 = 1 marks Thumb code.

! Some registers change when you read them: SPI_DR and USART_DR clear RXNE. Reading
! them from the debugger steals data from your program. The Peripherals tab skips them.""",
     "try": [("Vector table: SP and Reset_Handler", "mdw 0x08000000 2"), ("Flash size (KB)", "mdh 0x1FFFF7E0"),
             ("Unique ID", "mdw 0x1FFFF7E8 3"), ("First 16 bytes of SRAM", "mdb 0x20000000 16"),
             ("RCC_CR and RCC_CFGR", "read_memory 0x40021000 32 2")]},

    {"title": "6. Writing memory: LED by hand", "text": """\
    mww ADDR VALUE          write a word        (mwh / mwb: half-word / byte)
    mmw ADDR SETBITS CLEAR  read-modify-write: clear the CLEAR bits, then set SETBITS
    write_memory ADDR WIDTH {LIST}

Poking registers is the fastest way to learn a peripheral: write, then watch.
Exercise: light the PC13 LED with no code at all in main.c. Run the steps in order:

1. Enable the GPIOC clock: RCC_APB2ENR (0x40021018) bit 4 = IOPCEN
    mmw 0x40021018 0x10 0
2. PC13 = output push-pull 2 MHz: GPIOC_CRH (0x40011004) bits 23:20 = 0010
    mmw 0x40011004 0x00200000 0x00F00000
3. LED on - it is active low, so reset PC13: GPIOC_BSRR (0x40011010) bit 29 = BR13
    mww 0x40011010 0x20000000
4. LED off - set PC13: BSRR bit 13 = BS13
    mww 0x40011010 0x00002000

Open Peripherals -> GPIOC and watch CRH and ODR change as you go.
The same method works for the LCD: enable the clocks, configure PA2-PA7 and SPI1,
then check every field in the Peripherals tab before writing the C code.

! Your program may overwrite these registers at any moment; with an empty main loop
! it won't. A reset puts every register back to its reset value.""",
     "try": [("1. GPIOC clock on", "mmw 0x40021018 0x10 0"), ("2. PC13 output", "mmw 0x40011004 0x00200000 0x00F00000"),
             ("3. LED on", "mww 0x40011010 0x20000000"), ("4. LED off", "mww 0x40011010 0x00002000"),
             ("Read GPIOC_CRH, IDR, ODR", "mdw 0x40011004 3")]},

    {"title": "7. Core registers", "text": """\
The core registers can only be read while the CPU is halted.

    reg              all of them
    reg pc           one register
    reg r0 0x1234    write a register

    r0-r12   general purpose (r0-r3 carry function arguments and the return value)
    sp       stack pointer (r13); msp = main stack, psp = process stack (unused here)
    lr       link register (r14): return address. 0xFFFFFFF9 = "returning from an exception"
    pc       program counter (r15): the next instruction
    xPSR     flags N Z C V, and bits 8:0 = the exception being handled (0 = none)

Turn an address into a source line:

    arm-none-eabi-addr2line -f -e build/Debug/lcd.elf 0x08000290

The Core tab shows the registers with function names next to code addresses
(it reads the symbols from build/Debug/lcd.elf).""",
     "try": [("Halt", "halt"), ("PC", "reg pc"), ("SP", "reg sp"), ("xPSR", "reg xPSR"), ("All registers", "reg"),
             ("Resume", "resume")]},

    {"title": "8. Flash programming", "text": """\
    program build/Debug/lcd.elf verify reset      erase + write + verify, then run
    program build/Debug/lcd.bin 0x08000000 verify reset   a raw .bin needs an address
    verify_image build/Debug/lcd.elf              compare flash with the file
    flash banks                                   flash banks of the target
    flash info 0                                  pages and protection of bank 0
    stm32f1x mass_erase 0                         erase everything (halt first)
    stm32f1x options_read 0                       option bytes, read protection
    stm32f1x unlock 0                             remove read protection (ERASES flash)

How it works: OpenOCD halts the core, copies a small flash-loader routine into SRAM,
runs it on the chip to program flash pages (1 KB each on the F103C8), and checks the
result. The ELF file says where each section goes, so no address is needed for it.

The Flash tab has buttons for all of these, including Build.""",
     "try": [("Flash banks", "flash banks"), ("Flash info", "flash info 0"), ("Option bytes", "stm32f1x options_read 0")]},

    {"title": "9. Debugging with GDB", "text": """\
With OpenOCD running (connected here, or from a terminal), in another terminal:

    arm-none-eabi-gdb build/Debug/lcd.elf
    (gdb) target extended-remote localhost:3333
    (gdb) monitor reset halt          'monitor' passes any OpenOCD command through
    (gdb) load                        flash the ELF
    (gdb) break main
    (gdb) continue
    (gdb) next / step / finish        line by line, into a function, out of it
    (gdb) print my_variable
    (gdb) x/4wx 0x40013000            examine memory: 4 words, hex (SPI1 registers)
    (gdb) info registers
    (gdb) backtrace

GDB needs the .elf: it has the symbols and line numbers (-g3). The .bin has none.
arm-none-eabi-gdb comes with the STM32Cube bundle:
    ~/.local/share/stm32cube/bundles/gnu-tools-for-stm32/*/bin/

In VS Code, the STM32Cube extension (or Cortex-Debug) starts OpenOCD and GDB for you
when you press F5.""",
     "try": []},

    {"title": "10. Checking your LCD setup", "text": """\
When the screen stays white or black, check the hardware state step by step
instead of guessing. Each step is one read:

1. Are the clocks on?  RCC_APB2ENR: IOPAEN bit 2, IOPBEN bit 3, SPI1EN bit 12.
   RCC_APB1ENR: TIM3EN bit 1 (backlight PWM).
    mdw 0x40021018
2. Are the pins configured?  GPIOA_CRL (0x40010800) has one 4-bit nibble per pin:
   PA5 SCK and PA7 MOSI must be AF push-pull (0xB = 50 MHz),
   PA2 RESET, PA3 DC, PA4 CS general-purpose outputs (0x3 or 0x2), PA6 MISO input (0x4 or 0x8).
    mdw 0x40010800
3. Is SPI1 a master and enabled?  SPI1_CR1 (0x40013000): MSTR bit 2, SPE bit 6,
   BR bits 5:3 = speed, SSM + SSI bits 9 and 8 (needed because CS is a plain GPIO).
    mdw 0x40013000
4. Is SPI1 idle?  SPI1_SR (0x40013008): TXE = 1 and BSY = 0 when idle.
   MODF = 1 means master mode was lost (NSS) - set SSM and SSI.
    mdw 0x40013008
5. Pin levels: GPIOA_IDR (0x40010808). CS (PA4) high when idle, RESET (PA2) high.
    mdw 0x40010808
6. Backlight: duty = TIM3_CCR3 (0x4000043C) / (TIM3_ARR (0x4000042C) + 1).

The Peripherals tab decodes every field of these registers and marks the LCD pins
(read from Inc/board.h); the Chip tab has an "LCD wiring" summary.""",
     "try": [("APB2 clock enables", "mdw 0x40021018"), ("GPIOA_CRL (PA0-PA7 modes)", "mdw 0x40010800"),
             ("SPI1_CR1", "mdw 0x40013000"), ("SPI1_SR", "mdw 0x40013008"), ("GPIOA_IDR (levels)", "mdw 0x40010808"),
             ("TIM3 ARR and CCR3", "mdw 0x4000042C 5")]},

    {"title": "11. When it crashes (HardFault)", "text": """\
A bad pointer, a jump to an even address, a stack overflow... the core enters
HardFault_Handler. In this project that is Default_Handler (startup file): an
endless loop. Symptom: everything stops. Find out why:

    halt
    reg pc                  -> in Default_Handler?
    mdw 0xE000ED04          ICSR bits 8:0 = active exception: 3 = HardFault
    mdw 0xE000ED28          CFSR: the reason (MemManage / BusFault / UsageFault bits)
    mdw 0xE000ED2C          HFSR: FORCED = escalated from one of those
    mdw 0xE000ED38          BFAR: the bad address (if CFSR.BFARVALID)
    reg sp
    mdw <sp> 8              the hardware pushed r0 r1 r2 r3 r12 lr pc xPSR here:
                            that pc is the instruction that faulted

The Core tab's "Analyse fault" button does all of this and names the function.

Try it on purpose: halt, clear the Thumb bit (T, bit 24) of xPSR, resume. The
Cortex-M3 can only run Thumb code, so the next instruction raises a UsageFault
(INVSTATE), which escalates to HardFault (FORCED):
    halt
    reg xPSR 0
    resume
Then halt again and press Analyse fault in the Core tab. A reset run clears it.""",
     "try": [("Crash it on purpose", "halt; reg xPSR 0; resume"), ("Halt", "halt"),
             ("ICSR", "mdw 0xE000ED04"), ("CFSR", "mdw 0xE000ED28"), ("HFSR", "mdw 0xE000ED2C"), ("BFAR", "mdw 0xE000ED38")]},

    {"title": "12. Tcl scripting", "text": """\
Write your own commands with proc, and keep them in openocd.cfg:

    proc lcd_pins {} {
        set crl [read_memory 0x40010800 32 1]
        set out {}
        for {set p 2} {$p <= 7} {incr p} {
            append out [format "PA%d=0x%X " $p [expr {($crl >> ($p * 4)) & 0xF}]]
        }
        return $out
    }
    lcd_pins

Event hooks run code at the right moment, e.g. after every reset-init:

    stm32f1x.cpu configure -event reset-init { echo "reset-init done" }

`echo` writes to the OpenOCD log (the bottom panel); a proc's return value comes
back to whoever called it (telnet, this app, GDB 'monitor').

    openocd -f interface/stlink.cfg -f target/stm32f1x.cfg -c init -c "lcd_pins" -c shutdown
runs a script once and exits - handy in a Makefile or CI.""",
     "try": [("Define and run lcd_pins",
              "proc lcd_pins {} {set crl [read_memory 0x40010800 32 1]; set out {}; "
              "for {set p 2} {$p <= 7} {incr p} {append out [format \"PA%d=0x%X \" $p "
              "[expr {($crl >> ($p * 4)) & 0xF}]]}; return $out}; lcd_pins"),
             ("echo to the log", "echo {hello from Tcl}")]},

    {"title": "13. Troubleshooting", "text": """\
    Error: open failed  /  LIBUSB_ERROR_ACCESS
No permission for the USB device. Debian/Ubuntu's openocd package installs
/lib/udev/rules.d/60-openocd.rules: replug the ST-Link; your user may need the
plugdev group (sudo usermod -aG plugdev $USER, then log out and in).
    Error: open failed  (no device)
Probe not found: other USB port or cable. Check:  lsusb | grep 0483
    Target voltage: 0.0...
The board has no power, or the ST-Link's 3.3 V pin is not wired.
    UNEXPECTED idcode
A clone chip: tick "Clone chip" (set CPUTAPID 0), chapter 2.
    Error: init mode failed (unable to connect to the target)
    Failed to enter SWD mode            (the same problem, as st-info / st-flash say it)
The probe works and sees the voltage, but the core does not answer on SWD:
1. Wiring: SWDIO to DIO, SWCLK to CLK, GND to GND. Swapped DIO/CLK is the most common
   mistake; also try another jumper wire, they break inside.
2. Firmware turned SWD off (AFIO_MAPR.SWJ_CFG) or sleeps all the time: hold RESET,
   press Connect, release RESET about 1 s later. Or wire the ST-Link RST pin to the
   board's R pin and tick "Connect under reset".
3. Last resort: set the BOOT0 jumper to 1 and press RESET. The chip then starts ST's
   built-in bootloader instead of your program, so SWD is always free. Connect, do a
   Mass erase (Flash tab), set BOOT0 back to 0.
    Couldn't bind tcl to socket on port 6666: Address already in use
Another OpenOCD runs: use Attach, or stop it (pkill openocd).
    target not halted
That command (reg, flash erase) needs a halt first.
    Flash write fails / "device is protected"
Read protection is set: stm32f1x unlock 0 (this erases the flash), then power-cycle.
    Reads fail at random
Long jumper wires: lower the adapter speed (Speed box: 480 or 100 kHz).""",
     "try": [("Targets", "targets"), ("Adapter speed", "adapter speed")]},
]


# =============================================================================
# GUI
# =============================================================================

QUICK_ADDRESSES = [
    ("Flash / vector table", 0x08000000), ("SRAM", 0x20000000), ("Flash size + Unique ID", 0x1FFFF7E0),
    ("Option bytes", 0x1FFFF800), ("RCC", 0x40021000), ("GPIOA", 0x40010800), ("GPIOB", 0x40010C00),
    ("GPIOC", 0x40011000), ("SPI1", 0x40013000), ("TIM3", 0x40000400), ("SysTick", 0xE000E010),
    ("SCB", 0xE000ED00), ("DBGMCU", 0xE0042000),
]

CONSOLE_EXAMPLES = ["targets", "[target current] curstate", "halt", "resume", "step", "reset run", "reset halt",
                    "reg", "reg pc", "mdw 0x08000000 4", "mdw 0x20000000 8", "mdw 0x40013000 3",
                    "flash banks", "flash info 0", "adapter speed", "version", "help mdw"]


class App(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.title("OpenOCD Lab - STM32F103")
        self.geometry("1280x860")
        self.minsize(900, 600)
        self.uiq = queue.Queue()
        self.busy = set()
        self.labels = parse_board_pins()
        self.periphs = build_peripherals(self.labels)
        self.prev_vals = {}
        self.syms = Symbols()
        self.ocd = OpenOCD(self._log_ts, lambda: self.uiq.put(("lost",)))

        self._style()
        self._build_connection_bar()
        paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=4)
        self._build_log(paned)
        self._build_tutorial()
        self._build_chip()
        self._build_periph()
        self._build_core()
        self._build_memory()
        self._build_flash()
        self._build_console()
        self._build_status()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._pump)
        self.after(1000, self._auto_tick)
        self.log(f"OpenOCD Lab ready. LCD pins from {os.path.relpath(BOARD_H, HERE)}: "
                 + ", ".join(f"{v}=P{k[0]}{k[1]}" for k, v in sorted(self.labels.items())), "info")
        if os.path.isfile(args.elf):
            self.load_symbols(quiet=True)
        if args.connect:
            self.after(200, self.connect)
        elif args.attach:
            self.after(200, self.attach)

    # ---- look ---------------------------------------------------------------
    def _style(self):
        st = ttk.Style(self)
        if "clam" in st.theme_names():
            st.theme_use("clam")
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        self.mono.configure(size=10)
        base = tkfont.nametofont("TkDefaultFont")
        self.bold = base.copy()
        self.bold.configure(weight="bold")
        self.h1 = base.copy()
        self.h1.configure(size=base.cget("size") + 6, weight="bold")
        self.h2 = base.copy()
        self.h2.configure(size=base.cget("size") + 2, weight="bold")
        st.configure("Treeview", rowheight=22)
        st.configure("Treeview.Heading", font=self.bold)
        st.configure("Accent.TButton", font=self.bold)
        st.configure("Status.TLabel", padding=(6, 2))

    # ---- threading ------------------------------------------------------------
    def bg(self, fn, done=None, key=None):
        """Run fn() in a worker thread; done(result, error) later on the UI thread."""
        if key is not None:
            if key in self.busy:
                return
            self.busy.add(key)

        def work():
            try:
                res, err = fn(), None
            except Exception as e:      # reported in the UI, never crash the app
                res, err = None, e
            self.uiq.put(("done", done, res, err, key))
        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        try:
            while True:
                item = self.uiq.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1], item[2])
                elif kind == "lost":
                    self._set_connected(False)
                elif kind == "done":
                    _, done, res, err, key = item
                    if key is not None:
                        self.busy.discard(key)
                    if done:
                        done(res, err)
                    elif err:
                        self.error(err)
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _log_ts(self, text, tag="ocd"):
        self.uiq.put(("log", text, tag))

    def log(self, text, tag="info"):
        self._append_log(text, tag)

    def error(self, err):
        self.log(str(err), "err")
        self.status.set(str(err).splitlines()[0][:160])

    # ---- connection bar -------------------------------------------------------
    def _build_connection_bar(self):
        bar = ttk.Frame(self, padding=(6, 6, 6, 4))
        bar.pack(fill=tk.X)
        right = ttk.Frame(bar)
        right.pack(side=tk.RIGHT)          # packed first: never clipped when the window is narrow
        self.b_connect = ttk.Button(right, text="Connect", style="Accent.TButton", command=self.connect)
        self.b_connect.pack(side=tk.LEFT)
        self.b_attach = ttk.Button(right, text="Attach", command=self.attach)
        self.b_attach.pack(side=tk.LEFT, padx=4)
        self.b_disc = ttk.Button(right, text="Disconnect", command=self.disconnect, state=tk.DISABLED)
        self.b_disc.pack(side=tk.LEFT)
        self.conn_dot = tk.Label(right, text="● disconnected", fg="#b03030", width=14, anchor=tk.W)
        self.conn_dot.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(bar, text="Interface").pack(side=tk.LEFT)
        self.v_iface = tk.StringVar(value="interface/stlink.cfg")
        ttk.Combobox(bar, textvariable=self.v_iface, width=18,
                     values=["interface/stlink.cfg", "interface/stlink-dap.cfg", "interface/cmsis-dap.cfg",
                             "interface/jlink.cfg"]).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(bar, text="Target").pack(side=tk.LEFT)
        self.v_target = tk.StringVar(value="target/stm32f1x.cfg")
        ttk.Combobox(bar, textvariable=self.v_target, width=17,
                     values=["target/stm32f1x.cfg", "target/stm32f4x.cfg"]).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(bar, text="kHz").pack(side=tk.LEFT)
        self.v_speed = tk.StringVar(value="1000")
        ttk.Combobox(bar, textvariable=self.v_speed, width=5,
                     values=["100", "480", "1000", "1800", "4000"]).pack(side=tk.LEFT, padx=(4, 8))
        self.v_clone = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Clone chip", variable=self.v_clone).pack(side=tk.LEFT)
        self.v_srst = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Connect under reset", variable=self.v_srst).pack(side=tk.LEFT, padx=(6, 0))

    def openocd_args(self):
        args = ["openocd", "-f", self.v_iface.get()]
        if self.v_clone.get():
            args += ["-c", "set CPUTAPID 0"]
        args += ["-f", self.v_target.get()]
        args += ["-c", f"adapter speed {int(self.v_speed.get() or 1000)}"]
        if self.v_srst.get():
            args += ["-c", "reset_config srst_only srst_nogate connect_assert_srst"]
        args += ["-c", f"tcl_port {self.args.port}", "-c", "telnet_port 4444", "-c", "gdb_port 3333"]
        return args

    def connect(self):
        try:
            args = self.openocd_args()
        except ValueError:
            return self.error("Speed must be a number (kHz).")
        self.status.set("Starting OpenOCD...")
        self.b_connect.configure(state=tk.DISABLED)
        self.bg(lambda: self.ocd.start(args, self.args.port), self._connected, key="connect")

    def attach(self):
        self.status.set("Attaching...")
        self.bg(lambda: self.ocd.attach("127.0.0.1", self.args.port), self._connected, key="connect")

    def _connected(self, _res, err):
        if err:
            self._set_connected(False)
            if isinstance(err, ConnectionRefusedError):
                err = f"No OpenOCD on port {self.args.port}. Press Connect to start one."
            return self.error(err)
        self._set_connected(True)
        self.log("Connected. The Chip tab shows a summary; the log shows every command the app sends.", "info")
        self.refresh_chip()

    def disconnect(self):
        self.bg(self.ocd.disconnect, lambda r, e: self._set_connected(False))

    def _set_connected(self, on):
        self.conn_dot.configure(text="● connected" if on else "● disconnected", fg="#208040" if on else "#b03030")
        self.b_connect.configure(state=tk.DISABLED if on else tk.NORMAL)
        self.b_attach.configure(state=tk.DISABLED if on else tk.NORMAL)
        self.b_disc.configure(state=tk.NORMAL if on else tk.DISABLED)
        if not on:
            self.status.set("Disconnected")

    # ---- log ------------------------------------------------------------------
    def _build_log(self, paned):
        frame = ttk.Frame(paned)
        paned.add(frame, weight=1)
        head = ttk.Frame(frame)
        head.pack(fill=tk.X)
        ttk.Label(head, text="Log  (» = command sent by the app, grey = OpenOCD output)", font=self.bold).pack(side=tk.LEFT)
        ttk.Button(head, text="Clear", command=lambda: self.logw.delete("1.0", tk.END)).pack(side=tk.RIGHT)
        self.logw = scrolledtext.ScrolledText(frame, height=7, font=self.mono, wrap=tk.WORD)
        self.logw.pack(fill=tk.BOTH, expand=True)
        for tag, color in (("ocd", "#666666"), ("cmd", "#1f5fbf"), ("out", "#000000"), ("err", "#c0392b"),
                           ("warn", "#b9770e"), ("info", "#1e8449")):
            self.logw.tag_configure(tag, foreground=color)

    def _append_log(self, text, tag):
        self.logw.insert(tk.END, text + "\n", tag)
        lines = int(self.logw.index("end-1c").split(".")[0])
        if lines > 4000:
            self.logw.delete("1.0", f"{lines - 3000}.0")
        self.logw.see(tk.END)

    # ---- status bar -----------------------------------------------------------
    def _build_status(self):
        self.status = tk.StringVar(value="Not connected - press Connect (ST-Link plugged in, board powered)")
        self.chip_line = tk.StringVar(value="")
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(bar, textvariable=self.status, style="Status.TLabel").pack(side=tk.LEFT)
        ttk.Label(bar, textvariable=self.chip_line, style="Status.TLabel", font=self.bold).pack(side=tk.RIGHT)

    # ---- Tutorial -------------------------------------------------------------
    def _build_tutorial(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Tutorial  ")
        left = ttk.Frame(tab)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Chapters", font=self.bold).pack(anchor=tk.W)
        self.chapters = tk.Listbox(left, width=32, activestyle="none", exportselection=False)
        for ch in TUTORIAL:
            self.chapters.insert(tk.END, ch["title"])
        self.chapters.pack(fill=tk.Y, expand=True)
        self.chapters.bind("<<ListboxSelect>>", lambda e: self._show_chapter())
        nav = ttk.Frame(left)
        nav.pack(fill=tk.X, pady=4)
        ttk.Button(nav, text="< Previous", command=lambda: self._step_chapter(-1)).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(nav, text="Next >", command=lambda: self._step_chapter(1)).pack(side=tk.LEFT, expand=True, fill=tk.X)

        right = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        body = tkfont.nametofont("TkTextFont").copy()
        body.configure(size=11)
        self.tut_text = scrolledtext.ScrolledText(right, wrap=tk.WORD, padx=12, pady=8, relief=tk.FLAT,
                                                  background="#fbfbf8", font=body)
        right.add(self.tut_text, weight=4)
        t = self.tut_text
        t.tag_configure("h1", font=self.h1, spacing3=8)
        t.tag_configure("h2", font=self.h2, spacing1=10, spacing3=4)
        t.tag_configure("code", font=self.mono, background="#eef1f6", lmargin1=16, lmargin2=16)
        t.tag_configure("warn", foreground="#9a5b00", lmargin1=8)

        bottom = ttk.Frame(right)
        right.add(bottom, weight=2)
        ttk.Label(bottom, text="Try it on your board  (needs Connect)", font=self.bold).pack(anchor=tk.W)
        self.try_frame = ttk.Frame(bottom)
        self.try_frame.pack(fill=tk.X, pady=2)
        self.try_out = scrolledtext.ScrolledText(bottom, height=7, font=self.mono, wrap=tk.WORD)
        self.try_out.pack(fill=tk.BOTH, expand=True)
        self.try_out.tag_configure("cmd", foreground="#1f5fbf")
        self.try_out.tag_configure("err", foreground="#c0392b")
        self.chapters.selection_set(0)
        self._show_chapter()

    def _step_chapter(self, d):
        sel = self.chapters.curselection()
        i = max(0, min(len(TUTORIAL) - 1, (sel[0] if sel else 0) + d))
        self.chapters.selection_clear(0, tk.END)
        self.chapters.selection_set(i)
        self.chapters.see(i)
        self._show_chapter()

    def _show_chapter(self):
        sel = self.chapters.curselection()
        if not sel:
            return
        ch = TUTORIAL[sel[0]]
        t = self.tut_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        t.insert(tk.END, ch["title"] + "\n", "h1")
        for line in ch["text"].splitlines():
            if line.startswith("## "):
                t.insert(tk.END, line[3:] + "\n", "h2")
            elif line.startswith("    "):
                t.insert(tk.END, line[4:] + "\n", "code")
            elif line.startswith("! "):
                t.insert(tk.END, "⚠ " + line[2:] + "\n", "warn")
            else:
                t.insert(tk.END, line + "\n")
        t.configure(state=tk.DISABLED)
        for w in self.try_frame.winfo_children():
            w.destroy()
        if not ch["try"]:
            ttk.Label(self.try_frame, text="(this chapter is done in a terminal - nothing to run here)").pack(anchor=tk.W)
        for i, (label, command) in enumerate(ch["try"]):
            b = ttk.Button(self.try_frame, text=label, command=lambda c=command: self.try_command(c))
            b.grid(row=i // 4, column=i % 4, sticky=tk.EW, padx=2, pady=2)
        for c in range(4):
            self.try_frame.columnconfigure(c, weight=1)

    def try_command(self, command):
        out = self.try_out
        out.insert(tk.END, "» " + command + "\n", "cmd")
        out.see(tk.END)

        def done(res, err):
            if err:
                out.insert(tk.END, str(err) + "\n", "err")
            else:
                out.insert(tk.END, (res.rstrip() or "(no output - see the log below)") + "\n")
            out.see(tk.END)
        self.bg(lambda: self.ocd.cmd(command, timeout=30, echo=True), done)

    # ---- Chip -----------------------------------------------------------------
    def _build_chip(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Chip  ")
        self.chip_tab = tab
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Refresh", style="Accent.TButton", command=self.refresh_chip).pack(side=tk.LEFT)
        self.v_chip_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto refresh every second", variable=self.v_chip_auto).pack(side=tk.LEFT, padx=8)
        ttk.Label(bar, text="Instant view of the chip. Reads only registers that have no side effects.").pack(side=tk.LEFT, padx=8)
        self.chip_tree = self._tree(tab, ("value", "detail"), ("Value", "Where it comes from / what it means"), (360, 560), 230)

    def _tree(self, parent, cols, heads, widths, first=220):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        tree = ttk.Treeview(frame, columns=cols)
        tree.heading("#0", text="Item")
        tree.column("#0", width=first, stretch=False)
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, stretch=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        tree.tag_configure("section", font=self.bold, background="#e8edf5")
        tree.tag_configure("changed", foreground="#c0392b")
        tree.tag_configure("muted", foreground="#888888")
        tree.tag_configure("warn", foreground="#b9770e")
        return tree

    def refresh_chip(self):
        if not self.ocd.connected:
            return
        self.bg(lambda: collect_chip(self.ocd, self.labels, self.syms), self._show_chip, key="chip")

    def _show_chip(self, res, err):
        if err:
            return self.error(err)
        sections, summary, clocks = res
        if clocks:
            STATE.update({k: clocks[k] for k in STATE})
        tree = self.chip_tree
        y = tree.yview()[0]
        old = {}
        for sec in tree.get_children():
            for row in tree.get_children(sec):
                old[(tree.item(sec, "text"), tree.item(row, "text"))] = str(tree.item(row, "values")[0])
        tree.delete(*tree.get_children())
        for name, rows in sections:
            sid = tree.insert("", tk.END, text=name, open=True, tags=("section",))
            for item, value, detail in rows:
                prev = old.get((name, item))
                tags = ("changed",) if prev is not None and prev != str(value) else ()
                tree.insert(sid, tk.END, text=item, values=(value, detail), tags=tags)
        tree.yview_moveto(y)
        self.chip_line.set(summary)
        self.status.set("Chip info updated " + time.strftime("%H:%M:%S"))

    # ---- Peripherals ----------------------------------------------------------
    def _build_periph(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Peripherals  ")
        self.periph_tab = tab
        left = ttk.Frame(tab)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Peripheral", font=self.bold).pack(anchor=tk.W)
        self.plist = tk.Listbox(left, width=16, activestyle="none", exportselection=False, height=16)
        for p in self.periphs:
            self.plist.insert(tk.END, p.name + ("  [LCD]" if p.lcd else ""))
        self.plist.pack(fill=tk.Y, expand=True)
        self.plist.bind("<<ListboxSelect>>", lambda e: self.refresh_periph(reset=True))
        right = ttk.Frame(tab)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        bar = ttk.Frame(right)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Read", style="Accent.TButton", command=self.refresh_periph).pack(side=tk.LEFT)
        self.v_periph_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Auto refresh (red = changed)", variable=self.v_periph_auto).pack(side=tk.LEFT, padx=8)
        ttk.Label(bar, text="Write a register:").pack(side=tk.LEFT, padx=(20, 2))
        self.v_preg = tk.StringVar()
        self.preg_box = ttk.Combobox(bar, textvariable=self.v_preg, width=10, state="readonly")
        self.preg_box.pack(side=tk.LEFT)
        self.v_pval = tk.StringVar(value="0x")
        ttk.Entry(bar, textvariable=self.v_pval, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Write", command=self.write_periph).pack(side=tk.LEFT)
        self.pdesc = ttk.Label(right, text="", wraplength=900, justify=tk.LEFT)
        self.pdesc.pack(fill=tk.X, pady=(6, 0))
        self.ptree = self._tree(right, ("addr", "value", "meaning"), ("Address / bits", "Value", "Meaning"),
                                (110, 110, 620), 230)
        self.plist.selection_set(0)
        self._periph_selected()

    def _periph_selected(self):
        sel = self.plist.curselection()
        p = self.periphs[sel[0] if sel else 0]
        names = [r.name for r in p.regs if r.readable or r.name == "BSRR"]
        self.preg_box.configure(values=names)
        if self.v_preg.get() not in names:
            self.v_preg.set(names[0])
        clock = ""
        if p.clock:
            reg = "RCC_APB2ENR" if p.clock[0] == APB2ENR else "RCC_APB1ENR"
            clock = f"  Clock enable: {reg} bit {p.clock[1]}."
        self.pdesc.configure(text=f"{p.name} @ {hex32(p.base)} - {p.desc}{clock}")
        return p

    def refresh_periph(self, reset=False):
        p = self._periph_selected()
        if reset:
            self.ptree.delete(*self.ptree.get_children())
        if not self.ocd.connected:
            return
        self.bg(lambda: read_periph(self.ocd, p), lambda r, e: self._show_periph(p, r, e), key="periph")

    def _show_periph(self, p, res, err):
        if err:
            return self.error(err)
        clk, vals = res
        if "__clocks" in vals:
            c = vals.pop("__clocks")
            STATE.update({k: c[k] for k in STATE})
        tree = self.ptree
        y = tree.yview()[0]
        opened = {iid: tree.item(iid, "open") for iid in tree.get_children()}
        prev = self.prev_vals.get(p.name, {})
        tree.delete(*tree.get_children())
        if clk is False:
            tree.insert("", tk.END, iid="__clk", text="Clock is OFF", tags=("warn",),
                        values=("", "", "the peripheral has no clock: its registers read 0 and ignore writes. "
                                        "Enable it in RCC first."))
        if p.summary:
            rows = p.summary(vals)
            if rows:
                sid = tree.insert("", tk.END, iid="__summary", text="Summary", open=opened.get("__summary", True),
                                  tags=("section",), values=("", "", ""))
                for name, value in rows:
                    tree.insert(sid, tk.END, text=name, values=("", "", value))
        for r in p.regs:
            v = vals.get(r.name)
            addr = hex32(p.base + r.offset)
            if v is None:
                tree.insert("", tk.END, iid=r.name, text=r.name, values=(addr, "-", r.note), tags=("muted",))
                continue
            if isinstance(v, Exception):
                tree.insert("", tk.END, iid=r.name, text=r.name, values=(addr, "error", str(v)), tags=("warn",))
                continue
            old = prev.get(r.name)
            tags = ("changed",) if old is not None and old != v else ()
            rid = tree.insert("", tk.END, iid=r.name, text=r.name, values=(addr, hex32(v), r.note),
                              open=opened.get(r.name, True), tags=tags)
            for f in r.fields:
                fv = bits(v, f.lsb, f.width)
                if r.only_set and not fv:
                    continue
                ftags = ("changed",) if old is not None and bits(old, f.lsb, f.width) != fv else ()
                shown = str(fv) if f.width == 1 or fv < 10 else f"{fv} (0x{fv:X})"
                tree.insert(rid, tk.END, text="   " + f.name, values=(f.bitrange, shown, f.meaning(fv)), tags=ftags)
        tree.yview_moveto(y)
        self.prev_vals[p.name] = {k: v for k, v in vals.items() if isinstance(v, int)}
        self.status.set(f"{p.name} read " + time.strftime("%H:%M:%S"))

    def write_periph(self):
        p = self._periph_selected()
        reg = next((r for r in p.regs if r.name == self.v_preg.get()), None)
        try:
            value = int(self.v_pval.get(), 0)
        except ValueError:
            return self.error("Value must be a number, e.g. 0x1234 or 42.")
        if reg is None:
            return
        addr = p.base + reg.offset
        self.bg(lambda: self.ocd.write(addr, value, 32, echo=True), lambda r, e: self.error(e) if e else self.refresh_periph())

    # ---- Core -----------------------------------------------------------------
    def _build_core(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Core  ")
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X)
        for text, cmd in (("Halt", "halt"), ("Resume", "resume"), ("Step", "step"), ("Reset + run", "reset run"),
                          ("Reset + halt", "reset halt"), ("Reset init", "reset init")):
            ttk.Button(bar, text=text, command=lambda c=cmd: self.core_cmd(c)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Read registers", style="Accent.TButton", command=self.refresh_core).pack(side=tk.LEFT, padx=(16, 2))
        ttk.Button(bar, text="Analyse fault", command=self.fault).pack(side=tk.LEFT, padx=2)
        self.core_state = ttk.Label(bar, text="", font=self.bold)
        self.core_state.pack(side=tk.LEFT, padx=12)
        body = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        lf = ttk.Frame(body)
        body.add(lf, weight=1)
        self.core_tree = self._tree(lf, ("value", "meaning"), ("Value", "Meaning"), (110, 440), 90)
        rf = ttk.Frame(body)
        body.add(rf, weight=1)
        ttk.Label(rf, text="Fault analysis", font=self.bold).pack(anchor=tk.W, pady=(6, 0))
        self.fault_text = scrolledtext.ScrolledText(rf, font=self.mono, wrap=tk.NONE)
        self.fault_text.pack(fill=tk.BOTH, expand=True)
        self.fault_text.insert(tk.END, "Press 'Analyse fault' when the program seems to hang.\n"
                                       "It reads CFSR/HFSR/BFAR, unwinds the exception stack frame\n"
                                       "and names the function that crashed (tutorial chapter 11).")

    def core_cmd(self, command):
        def done(_r, err):
            if err:
                self.error(err)
            self.refresh_core()
        self.bg(lambda: self.ocd.cmd(command, timeout=20, echo=True), done)

    def refresh_core(self):
        if not self.ocd.connected:
            return

        def work():
            state = self.ocd.state()
            return state, (self.ocd.core_regs() if state == "halted" else [])
        self.bg(work, self._show_core, key="core")

    def _show_core(self, res, err):
        if err:
            return self.error(err)
        state, regs = res
        self.core_state.configure(text=f"CPU: {state}", foreground="#b9770e" if state == "halted" else "#208040")
        tree = self.core_tree
        tree.delete(*tree.get_children())
        if state != "halted":
            tree.insert("", tk.END, text="-", values=("", "Core registers can only be read while halted: press Halt."),
                        tags=("muted",))
            return
        for name, v in regs:
            if v is None:
                tree.insert("", tk.END, text=name, values=("?", ""), tags=("muted",))
                continue
            meaning = ""
            if name in ("pc", "lr") or (name.startswith("r") and 0x08000000 <= v < 0x08020000):
                if name == "lr" and (v & 0xFFFFFFF0) == 0xFFFFFFF0:
                    meaning = {0xFFFFFFF1: "EXC_RETURN: back to handler, MSP", 0xFFFFFFF9: "EXC_RETURN: back to thread, MSP",
                               0xFFFFFFFD: "EXC_RETURN: back to thread, PSP"}.get(v, "EXC_RETURN")
                else:
                    meaning = self.syms.lookup(v)
            elif name.lower() == "xpsr":
                flags = "".join(c if bits(v, b) else "-" for c, b in (("N", 31), ("Z", 30), ("C", 29), ("V", 28), ("Q", 27)))
                meaning = f"flags {flags}, Thumb={bits(v, 24)}, exception: {exception_name(bits(v, 0, 9))}"
            elif name in ("sp", "msp", "psp") and 0x20000000 <= v <= 0x20005000:
                meaning = f"SRAM, {0x20005000 - v} bytes below the stack top"
            elif name == "primask":
                meaning = "interrupts masked" if v & 1 else "interrupts enabled"
            elif name == "control":
                meaning = ("PSP" if bits(v, 1) else "MSP") + (", unprivileged" if bits(v, 0) else ", privileged")
            tree.insert("", tk.END, text=name, values=(hex32(v), meaning))

    def fault(self):
        def done(res, err):
            self.fault_text.delete("1.0", tk.END)
            self.fault_text.insert(tk.END, str(err) if err else res)
        self.bg(lambda: analyse_fault(self.ocd, self.syms), done, key="fault")

    # ---- Memory ---------------------------------------------------------------
    def _build_memory(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Memory  ")
        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Address").pack(side=tk.LEFT)
        self.v_addr = tk.StringVar(value="0x08000000")
        e = ttk.Entry(top, textvariable=self.v_addr, width=12)
        e.pack(side=tk.LEFT, padx=2)
        e.bind("<Return>", lambda ev: self.mem_read())
        ttk.Label(top, text="Count").pack(side=tk.LEFT, padx=(8, 0))
        self.v_count = tk.StringVar(value="64")
        ttk.Spinbox(top, from_=1, to=4096, textvariable=self.v_count, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text="Width").pack(side=tk.LEFT, padx=(8, 0))
        self.v_width = tk.StringVar(value="32")
        ttk.Combobox(top, textvariable=self.v_width, values=["8", "16", "32"], width=4, state="readonly").pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Read", style="Accent.TButton", command=self.mem_read).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="Jump to").pack(side=tk.LEFT, padx=(12, 2))
        self.v_quick = tk.StringVar()
        qb = ttk.Combobox(top, textvariable=self.v_quick, values=[f"{n}  {hex32(a)}" for n, a in QUICK_ADDRESSES],
                          width=30, state="readonly")
        qb.pack(side=tk.LEFT)
        qb.bind("<<ComboboxSelected>>", lambda ev: (self.v_addr.set(self.v_quick.get().split()[-1]), self.mem_read()))
        ttk.Label(top, text="   Write value").pack(side=tk.LEFT, padx=(12, 2))
        self.v_wval = tk.StringVar(value="0x")
        ttk.Entry(top, textvariable=self.v_wval, width=12).pack(side=tk.LEFT)
        ttk.Button(top, text="Write at Address", command=self.mem_write).pack(side=tk.LEFT, padx=4)

        body = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.dump = scrolledtext.ScrolledText(body, font=self.mono, wrap=tk.NONE)
        body.add(self.dump, weight=3)
        self.dump.insert(tk.END, "Hex dump. Words are shown as numbers (little-endian); the right column\n"
                                 "shows the same bytes as text. Peripheral registers need width 32.\n")
        side = ttk.Frame(body)
        body.add(side, weight=2)
        head = ttk.Frame(side)
        head.pack(fill=tk.X)
        ttk.Label(head, text="Variables in the ELF", font=self.bold).pack(side=tk.LEFT)
        ttk.Button(head, text="Reload", command=self.load_symbols).pack(side=tk.RIGHT)
        self.v_filter = tk.StringVar()
        fe = ttk.Entry(side, textvariable=self.v_filter)
        fe.pack(fill=tk.X, pady=2)
        self.v_filter.trace_add("write", lambda *a: self._fill_symbols())
        self.sym_tree = self._tree(side, ("addr", "size"), ("Address", "Size"), (100, 60), 200)
        self.sym_tree.bind("<Double-1>", lambda ev: self._read_symbol())
        self.sym_value = ttk.Label(side, text="Double-click a variable to read its live value.", wraplength=420,
                                   justify=tk.LEFT, font=self.mono)
        self.sym_value.pack(fill=tk.X, pady=4)

    def _parse_int(self, s, what):
        try:
            return int(s.replace("_", "").strip(), 0)
        except ValueError:
            raise OpenOCDError(f"{what}: not a number ({s!r}). Use 0x... for hex.")

    def mem_read(self):
        try:
            addr = self._parse_int(self.v_addr.get(), "Address")
            count = self._parse_int(self.v_count.get(), "Count")
            width = int(self.v_width.get())
        except OpenOCDError as e:
            return self.error(e)
        if width > 8 and addr % (width // 8):
            return self.error(f"Address must be a multiple of {width // 8} for {width}-bit reads.")
        self.bg(lambda: self.ocd.read(addr, width, min(count, 4096)), lambda r, e: self._show_dump(addr, width, r, e))

    def _show_dump(self, addr, width, vals, err):
        self.dump.delete("1.0", tk.END)
        if err:
            self.dump.insert(tk.END, str(err))
            return
        step = width // 8
        per_row = 16 // step
        lines = []
        for i in range(0, len(vals), per_row):
            row = vals[i:i + per_row]
            raw = b"".join(v.to_bytes(step, "little") for v in row)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
            cells = " ".join(f"{v:0{step * 2}X}" for v in row)
            lines.append(f"{addr + i * step:08X}:  {cells:<{per_row * (step * 2 + 1)}} |{text}|")
        self.dump.insert(tk.END, "\n".join(lines))

    def mem_write(self):
        try:
            addr = self._parse_int(self.v_addr.get(), "Address")
            value = self._parse_int(self.v_wval.get(), "Value")
            width = int(self.v_width.get())
        except OpenOCDError as e:
            return self.error(e)
        self.bg(lambda: self.ocd.write(addr, value, width, echo=True), lambda r, e: self.error(e) if e else self.mem_read())

    def load_symbols(self, quiet=False):
        path = self.v_elf.get() if hasattr(self, "v_elf") else self.args.elf

        def done(_r, err):
            if err:
                if not quiet:
                    self.error(err)
                return
            self.log(f"Loaded {len(self.syms.funcs)} functions and {len(self.syms.data)} variables from "
                     f"{os.path.relpath(path, HERE)}", "info")
            self._fill_symbols()
        self.bg(lambda: self.syms.load(path), done)

    def _fill_symbols(self):
        f = self.v_filter.get().lower()
        tree = self.sym_tree
        tree.delete(*tree.get_children())
        for name, addr, size, typ in self.syms.data:
            if f and f not in name.lower():
                continue
            if not (0x20000000 <= addr < 0x20005000 or 0x08000000 <= addr < 0x08020000):
                continue
            tree.insert("", tk.END, text=name, values=(hex32(addr), size))

    def _read_symbol(self):
        sel = self.sym_tree.selection()
        if not sel:
            return
        name = self.sym_tree.item(sel[0], "text")
        addr_s, size_s = self.sym_tree.item(sel[0], "values")
        addr, size = int(addr_s, 16), int(size_s) or 4
        width = 32 if size % 4 == 0 and addr % 4 == 0 else (16 if size % 2 == 0 and addr % 2 == 0 else 8)
        count = size // (width // 8)
        self.v_addr.set(hex32(addr))
        self.v_width.set(str(width))
        self.v_count.set(str(min(count, 4096)))

        def done(vals, err):
            if err:
                self.sym_value.configure(text=str(err))
                return
            self._show_dump(addr, width, vals, None)
            if count == 1:
                v = vals[0]
                signed = v - (1 << width) if v >> (width - 1) else v
                self.sym_value.configure(text=f"{name} = {v}  (0x{v:X}, signed {signed})")
            else:
                self.sym_value.configure(text=f"{name}: {size} bytes - see the dump on the left")
        self.bg(lambda: self.ocd.read(addr, width, min(count, 4096)), done)

    # ---- Flash ----------------------------------------------------------------
    def _build_flash(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  Flash  ")
        row = ttk.Frame(tab)
        row.pack(fill=tk.X)
        ttk.Label(row, text="ELF file").pack(side=tk.LEFT)
        self.v_elf = tk.StringVar(value=self.args.elf)
        ttk.Entry(row, textvariable=self.v_elf, width=70).pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse...", command=self._browse_elf).pack(side=tk.LEFT)

        actions = [
            ("Build (cmake Debug)", self.build, "cmake --preset Debug && cmake --build --preset Debug  (no OpenOCD needed)"),
            ("Program + verify + run", lambda: self.flash_cmd("program {%s} verify reset" % self.v_elf.get(), 120),
             "program build/Debug/lcd.elf verify reset"),
            ("Build, then program", self.build_and_program, "the two above in a row"),
            ("Verify only", lambda: self.flash_cmd("verify_image {%s}" % self.v_elf.get(), 60),
             "verify_image build/Debug/lcd.elf   (does flash match the file?)"),
            ("Flash info", lambda: self.flash_cmd("flash info 0", 20), "flash info 0   (size, pages, protection)"),
            ("Option bytes", lambda: self.flash_cmd("stm32f1x options_read 0", 20), "stm32f1x options_read 0"),
            ("Mass erase", self.mass_erase, "reset halt; stm32f1x mass_erase 0   (erases your program)"),
            ("Remove read protection", self.unlock, "stm32f1x unlock 0   (erases everything; power-cycle after)"),
        ]
        grid = ttk.Frame(tab)
        grid.pack(fill=tk.X, pady=12)
        for i, (text, fn, help_) in enumerate(actions):
            ttk.Button(grid, text=text, command=fn, width=26,
                       style="Accent.TButton" if i == 2 else "TButton").grid(row=i, column=0, sticky=tk.W, pady=3)
            ttk.Label(grid, text=help_, font=self.mono).grid(row=i, column=1, sticky=tk.W, padx=12)
        ttk.Label(tab, wraplength=1000, justify=tk.LEFT, text=(
            "Progress and errors appear in the log below. While this app is connected it owns the ST-Link, "
            "so ./flash.sh would fail - flash from here instead, or Disconnect first. "
            "After programming, the symbols are reloaded so the Core and Memory tabs name the new functions.")
        ).pack(anchor=tk.W, pady=8)

    def _browse_elf(self):
        path = filedialog.askopenfilename(initialdir=os.path.dirname(self.v_elf.get()) or HERE,
                                          filetypes=[("ELF", "*.elf"), ("All", "*")])
        if path:
            self.v_elf.set(path)
            self.load_symbols()

    def flash_cmd(self, command, timeout, then=None):
        def done(_r, err):
            if err:
                self.error(err)
            else:
                self.status.set("Done: " + command.split()[0])
                self.log("Done: " + command, "info")
                if command.startswith("program"):
                    self.load_symbols()
            if then:
                then(err)
        self.status.set("Running " + command.split()[0] + "...")
        self.bg(lambda: self.ocd.cmd(command, timeout=timeout, echo=True), done, key="flash")

    def build(self, then=None):
        def work():
            env = bundle_path_env()
            if not shutil.which("cmake", path=env["PATH"]):
                raise OpenOCDError("cmake not found (install the STM32Cube VS Code extension).")
            for step in (["cmake", "--preset", "Debug"], ["cmake", "--build", "--preset", "Debug"]):
                self._log_ts("$ " + " ".join(step), "cmd")
                p = subprocess.Popen(step, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    line = line.rstrip()
                    self._log_ts(line, "err" if "error" in line.lower() else ("warn" if "warning" in line.lower() else "ocd"))
                if p.wait() != 0:
                    raise OpenOCDError(f"Build failed ({' '.join(step)}): see the log.")

        def done(_r, err):
            if err:
                self.error(err)
            else:
                self.log("Build OK", "info")
                self.load_symbols()
            if then:
                then(err)
        self.status.set("Building...")
        self.bg(work, done, key="build")

    def build_and_program(self):
        self.build(then=lambda err: None if err else
                   self.flash_cmd("program {%s} verify reset" % self.v_elf.get(), 120))

    def mass_erase(self):
        if messagebox.askyesno("Mass erase", "Erase the whole flash? Your program will be gone until you flash it again."):
            self.flash_cmd("reset halt; stm32f1x mass_erase 0", 60)

    def unlock(self):
        if messagebox.askyesno("Remove read protection",
                               "This disables read protection AND erases the whole flash.\n"
                               "Power-cycle the board afterwards. Continue?"):
            self.flash_cmd("reset halt; stm32f1x unlock 0", 60)

    # ---- Console --------------------------------------------------------------
    def _build_console(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Console  ")
        self.console = scrolledtext.ScrolledText(tab, font=self.mono, wrap=tk.WORD)
        self.console.pack(fill=tk.BOTH, expand=True)
        self.console.tag_configure("cmd", foreground="#1f5fbf")
        self.console.tag_configure("err", foreground="#c0392b")
        self.console.insert(tk.END, "Any OpenOCD / Tcl command, exactly like telnet localhost 4444.\n"
                                    "Up/Down = history.  'help' lists every command, 'help mdw' explains one.\n\n")
        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row, text="> ", font=self.mono).pack(side=tk.LEFT)
        self.v_cmd = tk.StringVar()
        self.cmd_entry = ttk.Combobox(row, textvariable=self.v_cmd, values=CONSOLE_EXAMPLES, font=self.mono)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cmd_entry.bind("<Return>", lambda e: self.console_send())
        self.cmd_entry.bind("<Up>", lambda e: self._history(-1))
        self.cmd_entry.bind("<Down>", lambda e: self._history(1))
        ttk.Button(row, text="Send", style="Accent.TButton", command=self.console_send).pack(side=tk.LEFT, padx=4)
        self.history, self.hpos = [], 0

    def _history(self, d):
        if not self.history:
            return "break"
        self.hpos = max(0, min(len(self.history), self.hpos + d))
        self.v_cmd.set(self.history[self.hpos] if self.hpos < len(self.history) else "")
        self.cmd_entry.icursor(tk.END)
        return "break"

    def console_send(self):
        command = self.v_cmd.get().strip()
        if not command:
            return
        self.history.append(command)
        self.hpos = len(self.history)
        self.v_cmd.set("")
        self.console.insert(tk.END, "> " + command + "\n", "cmd")
        self.console.see(tk.END)

        def done(res, err):
            if err:
                self.console.insert(tk.END, str(err) + "\n", "err")
            elif res.strip():
                self.console.insert(tk.END, res.rstrip() + "\n")
            self.console.see(tk.END)
        self.bg(lambda: self.ocd.cmd(command, timeout=60), done)

    # ---- periodic work --------------------------------------------------------
    def _auto_tick(self):
        try:
            if self.ocd.connected:
                current = self.nb.select()
                if current == str(self.chip_tab) and self.v_chip_auto.get():
                    self.refresh_chip()
                elif current == str(self.periph_tab) and self.v_periph_auto.get():
                    self.refresh_periph()
        finally:
            self.after(1000, self._auto_tick)

    def on_close(self):
        try:
            self.ocd.disconnect()
        finally:
            self.destroy()


def main():
    ap = argparse.ArgumentParser(description="OpenOCD tutorial and live inspector for the STM32F103 Blue Pill.")
    ap.add_argument("--port", type=int, default=TCL_PORT, help="OpenOCD TCL port (default 6666)")
    ap.add_argument("--elf", default=DEFAULT_ELF, help="ELF with symbols (default build/Debug/lcd.elf)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--connect", action="store_true", help="start OpenOCD right away")
    g.add_argument("--attach", action="store_true", help="attach to an OpenOCD that already runs")
    args = ap.parse_args()
    App(args).mainloop()


if __name__ == "__main__":
    main()
