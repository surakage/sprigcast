"""Where a Windows process's "Memory" (Task Manager: private working set) actually sits.

Walks every committed region of this process and asks Windows, page by page, whether the page is
in RAM and private to us. The pages are then added up by what owns them:

- a library (DLL): the part of its code and data this process changed, so it can't be shared;
- a mapped file: the same for data files (fonts, caches);
- private memory: heaps and buffers, listed by the block Windows handed out (a 1080p RGBA frame
  is 8 MB, an NV12 one 3 MB, so sizes say a lot about who asked).

Windows only; everything is read-only and touches no other process.
"""

from __future__ import annotations

import sys
from collections import defaultdict

PAGE = 4096
MEM_COMMIT = 0x1000
MEM_IMAGE, MEM_MAPPED, MEM_PRIVATE = 0x1000000, 0x40000, 0x20000
CHUNK_PAGES = 1 << 16  # pages asked about per call (256 MB of address space)


def private_pages() -> dict:
    """{"libraries": {name: bytes}, "files": {name: bytes}, "blocks": [(reserved, in_ram, address)], "total": bytes}."""
    if sys.platform != "win32":
        raise RuntimeError("needs Windows")
    import ctypes
    from ctypes import wintypes

    import numpy as np

    class Region(ctypes.Structure):  # MEMORY_BASIC_INFORMATION (64-bit)
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    query = kernel32.VirtualQueryEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_size_t
    working_set = kernel32.K32QueryWorkingSetEx
    working_set.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    working_set.restype = wintypes.BOOL
    file_name = kernel32.K32GetMappedFileNameW
    file_name.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    file_name.restype = wintypes.DWORD
    process = kernel32.GetCurrentProcess()

    def in_ram_private(base: int, size: int) -> int:
        """Bytes of [base, base+size) that are in RAM and not shared with another process."""
        total = 0
        pages = size // PAGE
        for first in range(0, pages, CHUNK_PAGES):
            count = min(CHUNK_PAGES, pages - first)
            info = np.zeros((count, 2), dtype=np.uint64)  # (address, flags) pairs
            info[:, 0] = np.uint64(base + first * PAGE) + np.arange(count, dtype=np.uint64) * np.uint64(PAGE)
            if not working_set(process, info.ctypes.data, info.nbytes):
                continue
            flags = info[:, 1]
            valid = (flags & np.uint64(1)).astype(bool)
            shared = ((flags >> np.uint64(15)) & np.uint64(1)).astype(bool)
            total += int(np.count_nonzero(valid & ~shared)) * PAGE
        return total

    def name_of(address: int) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        got = file_name(process, address, buf, 1024)
        return buf.value.rsplit("\\", 1)[-1] if got else "(unnamed)"

    libraries: dict[str, int] = defaultdict(int)
    files: dict[str, int] = defaultdict(int)
    blocks: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # allocation base -> [reserved, private in RAM]
    region = Region()
    address, total = 0, 0
    while query(process, address, ctypes.byref(region), ctypes.sizeof(region)):
        base, size = region.BaseAddress or 0, region.RegionSize
        if region.Type == MEM_PRIVATE:
            blocks[region.AllocationBase or base][0] += size
        if region.State == MEM_COMMIT:
            private = in_ram_private(base, size)
            if private:
                total += private
                if region.Type == MEM_IMAGE:
                    libraries[name_of(base)] += private
                elif region.Type == MEM_MAPPED:
                    files[name_of(base)] += private
                else:
                    blocks[region.AllocationBase or base][1] += private
        address = base + size
        if address >= 0x7FFF_FFFF_0000:
            break
    return {
        "libraries": dict(libraries),
        "files": dict(files),
        "blocks": sorted(((reserved, used, base) for base, (reserved, used) in blocks.items() if used),
                         key=lambda b: -b[1]),
        "total": total,
    }


def size_buckets(blocks: list[tuple[int, int, int]]) -> list[tuple[str, int, int]]:
    """Private blocks grouped by how big the block is: (label, how many, bytes in RAM), biggest group first."""
    edges = [(1 << 20, "under 1 MB"), (4 << 20, "1-4 MB"), (16 << 20, "4-16 MB"), (64 << 20, "16-64 MB"),
             (256 << 20, "64-256 MB")]
    groups: dict[str, list[int]] = {}
    for reserved, used, _ in blocks:
        label = next((name for limit, name in edges if reserved < limit), "256 MB and up")
        group = groups.setdefault(label, [0, 0])
        group[0] += 1
        group[1] += used
    return sorted(((label, n, used) for label, (n, used) in groups.items()), key=lambda g: -g[2])


def report(when: str = "", top: int = 14) -> None:
    """Print the breakdown. Never raises: a tool that explains memory shouldn't end the run."""
    try:
        found = private_pages()
    except Exception as exc:
        print(f"  (couldn't break the memory down: {exc})", flush=True)
        return

    def mb(value: int) -> str:
        return f"{value / 1e6:6.1f} MB"

    lib_total, file_total = sum(found["libraries"].values()), sum(found["files"].values())
    block_total = sum(used for _, used, _ in found["blocks"])
    print(f"\n  Where the Task Manager memory sits{f', {when}' if when else ''} "
          f"({mb(found['total']).strip()} private, in RAM):", flush=True)
    print(f"    heaps and buffers {mb(block_total).strip()}, libraries' own data {mb(lib_total).strip()}, "
          f"mapped files {mb(file_total).strip()}", flush=True)
    print("    Libraries (the part only this app holds):", flush=True)
    for name, size in sorted(found["libraries"].items(), key=lambda kv: -kv[1])[:top]:
        print(f"      {mb(size)}  {name}", flush=True)
    if found["files"]:
        print("    Mapped files:", flush=True)
        for name, size in sorted(found["files"].items(), key=lambda kv: -kv[1])[:5]:
            print(f"      {mb(size)}  {name}", flush=True)
    print("    Heaps and buffers, by block size:", flush=True)
    for label, count, used in size_buckets(found["blocks"]):
        print(f"      {mb(used)}  in {count} block(s) of {label}", flush=True)
    print("    Biggest blocks (reserved -> in RAM):", flush=True)
    for reserved, used, _ in found["blocks"][:top]:
        print(f"      {mb(reserved)} -> {mb(used)}", flush=True)
