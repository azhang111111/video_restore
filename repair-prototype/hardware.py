"""Enumerate the actual Vulkan devices; names never select hard-coded behavior."""
import ctypes as C
import os


class ApplicationInfo(C.Structure):
    _fields_ = [("sType", C.c_uint32), ("pNext", C.c_void_p), ("pApplicationName", C.c_char_p),
                ("applicationVersion", C.c_uint32), ("pEngineName", C.c_char_p),
                ("engineVersion", C.c_uint32), ("apiVersion", C.c_uint32)]


class InstanceInfo(C.Structure):
    _fields_ = [("sType", C.c_uint32), ("pNext", C.c_void_p), ("flags", C.c_uint32),
                ("pApplicationInfo", C.POINTER(ApplicationInfo)), ("enabledLayerCount", C.c_uint32),
                ("ppEnabledLayerNames", C.c_void_p), ("enabledExtensionCount", C.c_uint32),
                ("ppEnabledExtensionNames", C.c_void_p)]


def detect_gpus() -> list[dict]:
    """Use the same Vulkan physical-device order that NCNN uses, not WMI order."""
    vk = C.WinDLL("vulkan-1.dll") if os.name == "nt" else C.CDLL("libvulkan.so.1")
    vk.vkCreateInstance.argtypes = [C.POINTER(InstanceInfo), C.c_void_p, C.POINTER(C.c_void_p)]
    vk.vkCreateInstance.restype = C.c_int32
    vk.vkEnumeratePhysicalDevices.argtypes = [C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_void_p)]
    vk.vkEnumeratePhysicalDevices.restype = C.c_int32
    vk.vkGetPhysicalDeviceProperties.argtypes = [C.c_void_p, C.c_void_p]
    vk.vkGetPhysicalDeviceProperties.restype = None
    vk.vkGetPhysicalDeviceMemoryProperties.argtypes = [C.c_void_p, C.c_void_p]
    vk.vkGetPhysicalDeviceMemoryProperties.restype = None
    vk.vkDestroyInstance.argtypes = [C.c_void_p, C.c_void_p]
    vk.vkDestroyInstance.restype = None
    app = ApplicationInfo(0, None, b"VideoRestore", 1, b"VideoRestore", 1, 1 << 22)
    info = InstanceInfo(1, None, 0, C.pointer(app), 0, None, 0, None)
    instance = C.c_void_p()
    if vk.vkCreateInstance(C.byref(info), None, C.byref(instance)) != 0:
        raise OSError("无法创建 Vulkan 实例，请检查显卡驱动。")
    try:
        count = C.c_uint32()
        if vk.vkEnumeratePhysicalDevices(instance, C.byref(count), None) != 0:
            raise OSError("无法枚举 Vulkan 设备。")
        devices = (C.c_void_p * count.value)()
        if vk.vkEnumeratePhysicalDevices(instance, C.byref(count), devices) not in (0, 5):
            raise OSError("无法读取 Vulkan 设备列表。")
        result = []
        for index, device in enumerate(devices[:count.value]):
            # Buffers exceed the Vulkan 1.x fixed structures, with 8-byte alignment.
            props = (C.c_uint64 * 512)()
            memory = (C.c_uint64 * 128)()
            vk.vkGetPhysicalDeviceProperties(device, C.byref(props))
            vk.vkGetPhysicalDeviceMemoryProperties(device, C.byref(memory))
            raw, mem = bytes(props), bytes(memory)
            name = raw[20:276].split(b"\0", 1)[0].decode("utf-8", errors="replace")
            kind = int.from_bytes(raw[16:20], "little")
            heaps = min(16, int.from_bytes(mem[260:264], "little"))
            local_bytes = sum(int.from_bytes(mem[264 + h * 16:272 + h * 16], "little")
                              for h in range(heaps) if int.from_bytes(mem[272 + h * 16:276 + h * 16], "little") & 1)
            result.append({"index": index, "name": name, "device_type": kind,
                           "device_local_bytes": local_bytes,
                           "vendor_id": int.from_bytes(raw[8:12], "little")})
        return result
    finally:
        vk.vkDestroyInstance(instance, None)


def select_gpu(devices: list[dict], requested: str = "auto") -> dict:
    if not devices:
        raise ValueError("未检测到可用 Vulkan 显卡，请安装对应显卡驱动。")
    if requested != "auto":
        index = int(requested)
        for device in devices:
            if device["index"] == index:
                return device
        raise ValueError(f"GPU {index} 当前不存在，请重新检测显卡。")
    # Prefer discrete hardware, then integrated hardware; break ties by local heap size.
    priority = {2: 4, 1: 3, 3: 2, 0: 1, 4: 0}
    return max(devices, key=lambda d: (priority.get(d["device_type"], 0), d.get("device_local_bytes", 0), -d["index"]))
