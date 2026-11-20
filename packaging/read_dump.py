"""Say where a crash dump (.dmp, written by SprigCast's crash handler) faulted: the exception, the module and
offset of the faulting address, and the modules the faulting thread's stack points into (no symbols needed)."""

import struct
import sys

from minidump.minidumpfile import MinidumpFile


def main(path: str) -> int:
    dump = MinidumpFile.parse(path)
    modules = sorted(((m.baseaddress, m.baseaddress + m.size, m.name.split("\\")[-1]) for m in dump.modules.modules))

    def where(address: int) -> str:
        for start, end, name in modules:
            if start <= address < end:
                return f"{name}+0x{address - start:x}"
        return ""

    reader = dump.get_reader()
    faulting = None
    if dump.exception is not None:
        for record in dump.exception.exception_records:
            rec = record.ExceptionRecord
            faulting = record.ThreadId
            print(f"exception 0x{rec.ExceptionCode_raw:08x} at 0x{rec.ExceptionAddress:x} {where(rec.ExceptionAddress)} "
                  f"thread {record.ThreadId} info {[hex(v) for v in rec.ExceptionInformation[:rec.NumberParameters]]}")
    for thread in dump.threads.threads:
        mark = " <- faulting" if thread.ThreadId == faulting else ""
        stack = thread.Stack
        print(f"thread {thread.ThreadId}{mark}: stack 0x{stack.StartOfMemoryRange:x} +{stack.MemoryLocation.DataSize}")
        try:
            data = reader.read(stack.StartOfMemoryRange, stack.MemoryLocation.DataSize)
        except Exception as exc:  # noqa: BLE001
            print("  (stack not read:", exc, ")")
            continue
        shown = 0
        limit = 160 if mark else 14
        for offset in range(0, len(data) - 7, 8):
            (value,) = struct.unpack_from("<Q", data, offset)
            name = where(value)
            if name:
                print(f"  +0x{offset:05x} {name}")
                shown += 1
                if shown >= limit:
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
