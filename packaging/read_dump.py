"""Say where a crash dump (crash *.dmp in the Logs folder, app/crash_dump.py) faulted, without symbols: the
faulting address and the stack's return addresses as module + nearest exported name, the registers, and what
the registers and the top of the stack point at when the dump holds that memory (SPRIGCAST_DUMP_MEMORY=1).

    uv run --no-project --with minidump --with pefile python packaging/read_dump.py <dump> <folder with the DLLs>
"""

import bisect
import glob
import os
import struct
import sys

import pefile
from minidump.minidumpfile import MinidumpFile

WANT = ("Qt6Core.dll", "Qt6Widgets.dll", "Qt6Gui.dll", "shiboken6.abi3.dll", "pyside6.abi3.dll", "QtCore.pyd",
        "QtWidgets.pyd", "QtGui.pyd", "python312.dll", "sprigcore.dll")
REGISTERS = {"rax": 0x78, "rcx": 0x80, "rdx": 0x88, "rbx": 0x90, "rsp": 0x98, "rbp": 0xA0, "rsi": 0xA8, "rdi": 0xB0,
             "r8": 0xB8, "r9": 0xC0, "r10": 0xC8, "r11": 0xD0, "r12": 0xD8, "r13": 0xE0, "r14": 0xE8, "r15": 0xF0,
             "rip": 0xF8}  # where each sits in Windows' x64 CONTEXT


def exports(folder: str) -> dict:
    out = {}
    for name in WANT:
        found = glob.glob(os.path.join(folder, "**", name), recursive=True)
        if not found:
            continue
        pe = pefile.PE(found[0], fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        table = sorted((e.address, (e.name or b"?").decode()) for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.address)
        out[name.lower()] = ([a for a, _ in table], [n for _, n in table])
    return out


def refs(dump, path: str, target: int, where) -> None:
    """Every place in the dump's memory that points at `target` (the object the crash was handed), with the
    words around it: one of them is Qt's note of the timer (its interval sits two words before the object)."""
    needle = struct.pack("<Q", target)
    segments = []
    for holder in (getattr(dump, "memory_segments_64", None), getattr(dump, "memory_segments", None)):
        segments += list(getattr(holder, "memory_segments", None) or [])
    print(f"places that point at 0x{target:x} ({len(segments)} blocks of memory in the dump):")
    shown = 0
    with open(path, "rb") as raw:
        for segment in segments:
            raw.seek(segment.start_file_address)
            blob = raw.read(segment.size)
            at = blob.find(needle)
            while at >= 0 and shown < 8:
                if at % 8 == 0:
                    shown += 1
                    print(f"  at 0x{segment.start_virtual_address + at:x}:")
                    for i in range(max(0, at - 56), min(len(blob) - 7, at + 32), 8):
                        (value,) = struct.unpack_from("<Q", blob, i)
                        print(f"    {(i - at) // 8:+d} {value:016x} {where(value)}")
                at = blob.find(needle, at + 1)


def main(path: str, folder: str) -> int:
    names = exports(folder)
    dump = MinidumpFile.parse(path)
    modules = sorted((m.baseaddress, m.baseaddress + m.size, m.name.split("\\")[-1]) for m in dump.modules.modules)

    def where(address: int) -> str:
        for start, end, name in modules:
            if start <= address < end:
                rva = address - start
                table = names.get(name.lower())
                if table:
                    i = bisect.bisect_right(table[0], rva) - 1
                    if i >= 0:
                        return f"{name}+0x{rva:x} {table[1][i]}+0x{rva - table[0][i]:x}"
                return f"{name}+0x{rva:x}"
        return ""

    reader = dump.get_reader()

    def peek(address: int, size: int):
        try:
            return reader.read(address, size)
        except Exception:  # noqa: BLE001 (not in the dump)
            return None

    def words(address: int, count: int, indent: str) -> None:
        blob = peek(address, 8 * count)
        if blob is None:
            print(f"{indent}(0x{address:x}: not in the dump)")
            return
        for i in range(0, len(blob), 8):
            (value,) = struct.unpack_from("<Q", blob, i)
            print(f"{indent}+0x{i:02x} {value:016x} {where(value)}")

    if dump.exception is None:
        print("no exception in this dump")
        return 0
    record = dump.exception.exception_records[0]
    rec = record.ExceptionRecord
    print(f"exception 0x{rec.ExceptionCode_raw:08x} {list(rec.ExceptionInformation)[:rec.NumberParameters]} at {where(rec.ExceptionAddress)} on thread {record.ThreadId}")
    registers = {}
    try:
        with open(path, "rb") as raw:
            raw.seek(record.ThreadContext.Rva)
            context = raw.read(record.ThreadContext.DataSize)
        registers = {name: struct.unpack_from("<Q", context, at)[0] for name, at in REGISTERS.items()}
    except Exception as exc:  # noqa: BLE001
        print("registers not read:", exc)
    for name, value in registers.items():
        print(f"  {name} {value:016x} {where(value)}")
    for name in ("rcx", "rbx"):
        if name in registers and registers[name] > 0x10000 and not where(registers[name]):
            print(f"what {name} points at:")
            words(registers[name], 12, "  ")
    target = registers.get("rcx", 0)
    if target > 0x10000 and not where(target):
        refs(dump, path, target, where)
    for thread in dump.threads.threads:
        if thread.ThreadId != record.ThreadId:
            continue
        stack = thread.Stack
        data = reader.read(stack.StartOfMemoryRange, stack.MemoryLocation.DataSize)
        first = max(0, registers.get("rsp", stack.StartOfMemoryRange) - stack.StartOfMemoryRange)
        print("the stack from where it faulted (return addresses, named; --words shows the words between too):")
        shown = 0
        for offset in range(first, len(data) - 7, 8):
            (value,) = struct.unpack_from("<Q", data, offset)
            name = where(value)
            if name:
                print(f"  +0x{offset - first:04x} {name}")
                shown += 1
                if shown >= 60:
                    break
            elif offset - first < 0x140 and "--words" in sys.argv:
                print(f"  +0x{offset - first:04x} {value:016x}")
                if value > 0x10000 and peek(value, 8) is not None:
                    words(value, 8, "        ")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
