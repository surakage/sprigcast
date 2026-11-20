"""Temporary: say where a crash dump faulted, with Qt's exported names."""

import bisect
import glob
import os
import struct
import sys

import pefile
from minidump.minidumpfile import MinidumpFile

WANT = ("Qt6Core.dll", "Qt6Widgets.dll", "Qt6Gui.dll", "shiboken6.abi3.dll", "pyside6.abi3.dll", "QtCore.pyd",
        "QtWidgets.pyd", "python312.dll")


def exports(folder: str) -> dict:
    out = {}
    for name in WANT:
        found = glob.glob(os.path.join(folder, "**", name), recursive=True)
        if not found:
            continue
        pe = pefile.PE(found[0], fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        table = sorted((e.address, (e.name or b"?").decode()) for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.address)
        out[name.lower()] = ([a for a, _ in table], [n for _, n in table], pe.FILE_HEADER.TimeDateStamp)
    return out


def main(path: str, folder: str) -> int:
    names = exports(folder)
    dump = MinidumpFile.parse(path)
    modules = sorted((m.baseaddress, m.baseaddress + m.size, m.name.split("\\")[-1], m.timestamp)
                     for m in dump.modules.modules)
    for _s, _e, name, stamp in modules:
        if name.lower() in names:
            print(f"{name}: dump timestamp {stamp:x}, local {names[name.lower()][2]:x}")

    def where(address: int) -> str:
        for start, end, name, _stamp in modules:
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
    faulting = None
    for record in dump.exception.exception_records:
        rec = record.ExceptionRecord
        faulting = record.ThreadId
        print(f"exception 0x{rec.ExceptionCode_raw:08x} at {where(rec.ExceptionAddress)} thread {record.ThreadId}")
    receiver = 0x22094c4ff80

    def peek(address: int, size: int = 0x60):
        try:
            return reader.read(address, size)
        except Exception:  # noqa: BLE001
            return None

    got = peek(receiver)
    print("receiver memory:", "not in the dump" if got is None else "")
    if got is not None:
        for i in range(0, len(got), 8):
            (v,) = struct.unpack_from("<Q", got, i)
            print(f"  +0x{i:02x} {v:016x} {where(v)}")
    for thread in dump.threads.threads:
        if thread.ThreadId != faulting:
            continue
        stack = thread.Stack
        data = reader.read(stack.StartOfMemoryRange, stack.MemoryLocation.DataSize)
        for offset in range(0x10d0, 0x1300, 8):
            (value,) = struct.unpack_from("<Q", data, offset)
            note = where(value)
            blob = None if note else peek(value, 0x40)
            if blob is not None and struct.pack("<Q", receiver) in blob:
                note = "POINTS AT A BLOCK HOLDING THE RECEIVER: " + " ".join(
                    f"{struct.unpack_from('<Q', blob, i)[0]:x}" for i in range(0, 0x40, 8))
            print(f"  stack +0x{offset:05x} {value:016x} {note}")
    return 0
