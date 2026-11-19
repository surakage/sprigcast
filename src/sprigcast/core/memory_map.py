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
import time

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
    for base, size, state, kind, *_ in regions:
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


PROTECT = {0x01: "no access", 0x02: "read-only", 0x04: "read-write", 0x08: "copy-on-write", 0x10: "execute",
           0x20: "execute-read", 0x40: "execute-read-write", 0x80: "execute-copy"}


def protect_name(protect: int) -> str:
    """PAGE_* in words. Write-combined is how a graphics driver maps memory the card reads (upload buffers)."""
    name = PROTECT.get(protect & 0xFF, f"protect {protect:#x}")
    if protect & 0x400:
        name += " write-combined"
    elif protect & 0x200:
        name += " uncached"
    if protect & 0x100:
        name += " guard"
    return name


def big_blocks(regions, heaps=frozenset(), top: int = 8) -> str:
    """The private blocks of BIG or more, biggest first, each with the protection it was allocated with and
    whether it is one of the process's heaps ("heap": its base is a GetProcessHeaps handle). Who made a block
    can't be read from here; the protection tells driver memory (write-combined), JIT code (execute-read-write)
    and ordinary allocations (read-write) apart."""
    sizes: dict[int, int] = collections.defaultdict(int)
    protects: dict[int, int] = {}
    for base, size, state, kind, *more in regions:
        if state == MEM_COMMIT and kind == MEM_PRIVATE:
            sizes[base] += size
            if more:
                protects.setdefault(base, more[0])
    big = sorted(((size, base) for base, size in sizes.items() if size >= BIG), reverse=True)[:top]
    parts = []
    for size, base in big:
        words = [f"{size / MB:.0f} MB"]
        if base in protects:
            words.append(protect_name(protects[base]))
        if base in heaps:
            words.append("heap")
        parts.append(" ".join(words))
    return ", ".join(parts)


def program_files(regions, name_of, top: int = 6) -> str:
    """The modules (DLLs) with the most committed image memory: "Qt6WebEngineCore.dll 180 MB, ..."."""
    sizes: dict[int, int] = collections.defaultdict(int)
    for base, size, state, kind, *_ in regions:
        if state == MEM_COMMIT and kind == MEM_IMAGE:
            sizes[base] += size
    named: dict[str, int] = collections.defaultdict(int)
    for base, size in sizes.items():
        named[name_of(base) or "unnamed"] += size
    biggest = sorted(named.items(), key=lambda kv: -kv[1])[:top]
    return ", ".join(f"{name} {size / MB:.0f} MB" for name, size in biggest)


def windows_heaps() -> frozenset[int]:
    """The base addresses of this process's heaps (GetProcessHeaps)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    get = kernel32.GetProcessHeaps
    get.restype, get.argtypes = wintypes.DWORD, [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    count = get(0, None)
    if not count:
        return frozenset()
    handles = (ctypes.c_void_p * (count + 16))()
    count = min(get(len(handles), handles), len(handles))
    return frozenset(h for h in handles[:count] if h)


def module_name(base: int) -> str | None:
    """The file name of the module loaded at `base` (an image's allocation base is its module handle)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    path = ctypes.create_unicode_buffer(520)
    if not kernel32.GetModuleFileNameW(ctypes.c_void_p(base), path, 520):
        return None
    return path.value.rsplit("\\", 1)[-1]


def windows_regions():
    """(allocation base, size, state, type, allocation protect) for every region of this process's address
    space."""
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
        yield info.AllocationBase or 0, info.RegionSize, info.State, info.Type, info.AllocationProtect
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


def memory_map_line(reason: str = "") -> str | None:
    """"memory map: private ... ; big blocks ...; graphics driver: ... on the card, ... in system memory", or None
    off Windows. `reason` says when it was taken ("before Twitch's encoder opens")."""
    if sys.platform != "win32":
        return None
    parts = []
    try:
        regions = list(windows_regions())
        parts.append(summarize(regions))
        try:
            heaps = windows_heaps()
        except Exception:  # noqa: BLE001
            heaps = frozenset()
        blocks = big_blocks(regions, heaps)
        if blocks:
            parts.append("big blocks " + blocks)
        try:
            modules = program_files(regions, module_name)
        except Exception:  # noqa: BLE001 (the rest of the line still counts)
            modules = ""
        if modules:
            parts.append("biggest program files " + modules)
    except Exception as exc:  # noqa: BLE001 (a diagnostic: never in the way)
        parts.append(f"address space unreadable ({exc})")
    try:
        graphics = graphics_memory()
    except Exception:  # noqa: BLE001
        graphics = None
    if graphics is not None:
        parts.append(f"graphics driver {graphics[0] / MB:.0f} MB on the card, {graphics[1] / MB:.0f} MB in system memory")
    return f"memory map{f' ({reason})' if reason else ''}: " + "; ".join(parts)


def log_memory_map(reason: str = "", after_s: float = 0.0) -> None:
    """Log the memory map from a thread of its own (the window never waits for the walk), `after_s` from now."""
    def run():
        if after_s > 0:
            time.sleep(after_s)
        try:
            line = memory_map_line(reason)
        except Exception:  # noqa: BLE001
            return
        if line:
            log.info(line)

    if sys.platform == "win32":
        threading.Thread(target=run, name="memory map", daemon=True).start()


ENCODER_AFTER_S = 10.0  # the map again this long after an encoder starts: the driver's session is set up by then
_encoder_logged = 0.0


def around_encoder(name: str) -> None:
    """An encoder is about to start: the map now and ENCODER_AFTER_S later, so a block can be tied to that moment
    (light-PC test 2026-11-19: going live added 32, 32 and 21 MB blocks nobody could name). Encoders starting
    together (a stream and its recording) share one pair."""
    global _encoder_logged
    now = time.monotonic()
    if now - _encoder_logged < 2.0:
        return
    _encoder_logged = now
    log_memory_map(f"before {name} starts")
    log_memory_map(f"{ENCODER_AFTER_S:.0f} s after {name} started", ENCODER_AFTER_S)
