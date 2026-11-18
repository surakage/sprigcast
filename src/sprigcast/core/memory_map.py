"""Where SprigCast's committed memory is, beyond what Python itself can see: for the 10-minute memory line.

Task Manager's figure (the private working set) drops when Windows is handed unused pages at rest and climbs again
as pages are touched, so it alone can't tell "holds more" from "touched more". This walks the process's address
space (VirtualQuery) and says how much is committed privately, in how many blocks and how big the biggest are,
how much is shared or mapped (a browser page's frame block, the graphics driver's shared memory), and how much
the graphics card's driver says this process uses, on the card and in system memory (DXGI QueryVideoMemoryInfo).
The 2026-11-18 native engine start sat at about 1 GB committed with no way to say what held it.

Runs on its own thread: walking a few thousand regions takes some milliseconds.
"""

from __future__ import annotations

import collections
import logging
import sys
import threading

log = logging.getLogger(__name__)

MEM_COMMIT = 0x1000
MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE = 0x20000, 0x40000, 0x1000000
MB = 1024 * 1024
BIG = 16 * MB  # private blocks this size or more are counted on their own (frame buffers, driver memory)


def summarize(regions) -> str:
    """regions: (allocation base, size, state, type) for each region. A private block is everything committed with
    the same allocation base (one VirtualAlloc: a heap segment, a driver allocation, a big numpy array)."""
    private: dict[int, int] = collections.defaultdict(int)
    mapped = image = 0
    for base, size, state, kind in regions:
        if state != MEM_COMMIT:
            continue
        if kind == MEM_PRIVATE:
            private[base] += size
        elif kind == MEM_MAPPED:
            mapped += size
        elif kind == MEM_IMAGE:
            image += size
    blocks = sorted(private.values(), reverse=True)
    big = [b for b in blocks if b >= BIG]
    biggest = ", ".join(f"{b / MB:.0f}" for b in blocks[:6])
    return (f"private {sum(blocks) / MB:.0f} MB in {len(blocks):,} blocks (biggest {biggest or '-'} MB; "
            f"{sum(big) / MB:.0f} MB in {len(big)} of {BIG // MB} MB or more), shared {mapped / MB:.0f} MB, "
            f"program files {image / MB:.0f} MB")


def windows_regions():
    """(allocation base, size, state, type) for every region of this process's address space."""
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):  # MEMORY_BASIC_INFORMATION (64-bit)
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD)]

    query = ctypes.WinDLL("kernel32").VirtualQuery  # type: ignore[attr-defined]
    query.restype = ctypes.c_size_t
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    info, address, end = Info(), 0, 1 << 47
    for _ in range(1_000_000):  # never endless, whatever Windows answers
        if address >= end or not query(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)):
            break
        if not info.RegionSize:
            break
        yield info.AllocationBase or 0, info.RegionSize, info.State, info.Type
        address = (info.BaseAddress or 0) + info.RegionSize



def graphics_memory() -> tuple[int, int] | None:
    """(on the card, in system memory) bytes this process has with the graphics driver, all cards together, or
    None when Windows can't say (before Windows 10, or no DXGI 1.4)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import c_uint, c_uint64, c_void_p

    class Info(ctypes.Structure):  # DXGI_QUERY_VIDEO_MEMORY_INFO
        _fields_ = [("Budget", c_uint64), ("CurrentUsage", c_uint64), ("AvailableForReservation", c_uint64),
                    ("CurrentReservation", c_uint64)]

    class Guid(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    def guid(a, b, c, *d):
        return Guid(a, b, c, (ctypes.c_ubyte * 8)(*d))

    factory_iid = guid(0x770AAE78, 0xF26F, 0x4DBA, 0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)  # IDXGIFactory1
    adapter3_iid = guid(0x645967A4, 0x1392, 0x4310, 0xA7, 0x98, 0x80, 0x53, 0xCE, 0x3E, 0x93, 0xFD)  # IDXGIAdapter3

    def method(obj, index, *argtypes):
        vtable = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(c_void_p))[0], ctypes.POINTER(c_void_p))
        return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(vtable[index])  # type: ignore[attr-defined]

    def release(obj):
        if obj:
            method(obj, 2)(obj)

    try:
        create = ctypes.WinDLL("dxgi").CreateDXGIFactory1  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None
    create.restype, create.argtypes = ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p]
    factory = c_void_p()
    if create(ctypes.byref(factory_iid), ctypes.byref(factory)) < 0 or not factory:
        return None
    local = system = 0
    found = False
    try:
        for index in range(8):
            adapter = c_void_p()
            if method(factory, 12, c_uint, c_void_p)(factory, index, ctypes.byref(adapter)) < 0:  # EnumAdapters1
                break
            three = c_void_p()
            try:
                if method(adapter, 0, c_void_p, c_void_p)(adapter, ctypes.byref(adapter3_iid), ctypes.byref(three)) < 0:
                    continue
                for group in (0, 1):  # DXGI_MEMORY_SEGMENT_GROUP_LOCAL, _NON_LOCAL
                    info = Info()
                    if method(three, 14, c_uint, c_uint, c_void_p)(three, 0, group, ctypes.byref(info)) >= 0:
                        found = True
                        if group == 0:
                            local += info.CurrentUsage
                        else:
                            system += info.CurrentUsage
            finally:
                release(three)
                release(adapter)
    finally:
        release(factory)
    return (local, system) if found else None


def memory_map_line() -> str | None:
    """"memory map: private ... ; graphics driver: ... on the card, ... in system memory", or None off Windows."""
    if sys.platform != "win32":
        return None
    parts = []
    try:
        parts.append(summarize(windows_regions()))
    except Exception as exc:  # noqa: BLE001 (a diagnostic: never in the way)
        parts.append(f"address space unreadable ({exc})")
    try:
        graphics = graphics_memory()
    except Exception:  # noqa: BLE001
        graphics = None
    if graphics is not None:
        parts.append(f"graphics driver {graphics[0] / MB:.0f} MB on the card, {graphics[1] / MB:.0f} MB in system memory")
    return "memory map: " + "; ".join(parts)


def log_memory_map() -> None:
    """Log the memory map from a thread of its own (the window never waits for the walk)."""
    def run():
        try:
            line = memory_map_line()
        except Exception:  # noqa: BLE001
            return
        if line:
            log.info(line)

    if sys.platform == "win32":
        threading.Thread(target=run, name="memory map", daemon=True).start()
