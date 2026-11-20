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
    qt = ("qt6", "shiboken", "pyside", "qtcore.pyd", "qtwidgets.pyd", "qtgui.pyd")
    for thread in dump.threads.threads:
        stack = thread.Stack
        try:
            data = reader.read(stack.StartOfMemoryRange, stack.MemoryLocation.DataSize)
        except Exception as exc:  # noqa: BLE001
            print(f"thread {thread.ThreadId}: stack not read: {exc}")
            continue
        found = []
        for offset in range(0, len(data) - 7, 8):
            (value,) = struct.unpack_from("<Q", data, offset)
            name = where(value)
            if name:
                found.append((offset, name))
        is_faulting = thread.ThreadId == faulting
        if is_faulting:
            print(f"thread {thread.ThreadId} <- faulting")
            for offset, name in [f for f in found if f[0] >= 0x10d0][:60]:
                print(f"  +0x{offset:05x} {name}")
        elif any(n.lower().startswith(qt) for _o, n in found[:40]):
            print(f"thread {thread.ThreadId}: Qt near the top of its stack")
            for offset, name in found[:28]:
                print(f"  +0x{offset:05x} {name}")
        else:
            print(f"thread {thread.ThreadId}: " + " | ".join(n.split(" ")[0] for _o, n in found[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
