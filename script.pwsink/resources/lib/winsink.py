import argparse
import ctypes
import json
import logging
import sys
import time
import uuid
from contextlib import ExitStack

LOGGER = logging.getLogger(__name__)

_ACTIVE = 1
_UNPLUGGED = 8
_NOT_FOUND = 0x80070490
_CHANGED_MODE = 0x80010106
_POINTER = ctypes.c_void_p
_UINT = ctypes.c_uint32
_HRESULT = ctypes.c_int32
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class GUID(ctypes.Structure):
    _fields_ = [("data1", _UINT), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def __str__(self):
        return str(uuid.UUID(bytes_le=bytes(self)))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("format", GUID), ("id", _UINT)]


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("set", GUID), ("id", _UINT), ("flags", _UINT)]


class _Blob(ctypes.Structure):
    _fields_ = [("size", _UINT), ("data", _POINTER)]


class _VariantValue(ctypes.Union):
    _fields_ = [("pointer", _POINTER), ("blob", _Blob), ("integer", ctypes.c_int64),
                ("unsigned", _UINT)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint16), ("reserved1", ctypes.c_uint16),
                ("reserved2", ctypes.c_uint16), ("reserved3", ctypes.c_uint16),
                ("value", _VariantValue)]


class _WindowsError(RuntimeError):
    def __init__(self, operation, code):
        self.code = code & 0xffffffff
        super().__init__(f"{operation} failed (0x{self.code:08X})")


def _check(result, operation):
    if result < 0:
        raise _WindowsError(operation, result)


def _invoke(pointer, index, result_type, argtypes, *args):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_POINTER))).contents
    function = _WINFUNCTYPE(result_type, _POINTER, *argtypes)(table[index])
    return function(pointer, *args)


def _call(pointer, index, operation, argtypes, *args):
    _check(_invoke(pointer, index, _HRESULT, argtypes, *args), operation)


def _release(pointer):
    if pointer and pointer.value:
        _invoke(pointer, 2, _UINT, ())


class _Endpoint:
    def __init__(self, id, name, interface_name, container, state, filters, form_factor=None):
        self.id = id
        self.name = name
        self.interface_name = interface_name
        self.container = container
        self.state = state
        self.filters = filters
        self.form_factor = form_factor


class _AudioAPI:
    def __enter__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise RuntimeError("Windows audio APIs are only available on Windows")
        LOGGER.debug("Initializing Windows Core Audio")
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = [_POINTER, _UINT]
        self.ole32.CoInitializeEx.restype = _HRESULT
        self.ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), _POINTER, _UINT,
                                                 ctypes.POINTER(GUID), ctypes.POINTER(_POINTER)]
        self.ole32.CoCreateInstance.restype = _HRESULT
        self.ole32.CoTaskMemFree.argtypes = [_POINTER]
        self.ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        self.ole32.PropVariantClear.restype = _HRESULT
        self.stack = ExitStack()
        result = self.ole32.CoInitializeEx(None, 0)
        LOGGER.debug(f"CoInitializeEx returned 0x{result & 0xffffffff:08X}")
        if result >= 0:
            self.stack.callback(self.ole32.CoUninitialize)
        elif result & 0xffffffff != _CHANGED_MODE:
            _check(result, "CoInitializeEx")
        try:
            self.enumerator = self._create(
                "bcde0395-e52f-467c-8e3d-c4579291692e",
                "a95664d2-9614-4f35-a746-de8db63617e6", self.stack)
        except Exception:
            self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        self.stack.close()

    def _create(self, class_id, interface_id, stack):
        class_guid = GUID.from_string(class_id)
        interface_guid = GUID.from_string(interface_id)
        pointer = _POINTER()
        _check(self.ole32.CoCreateInstance(ctypes.byref(class_guid), None, 23,
                                           ctypes.byref(interface_guid), ctypes.byref(pointer)),
               "CoCreateInstance")
        stack.callback(_release, pointer)
        return pointer

    def _object(self, parent, index, operation, argtypes, args, stack):
        pointer = _POINTER()
        _call(parent, index, operation, (*argtypes, ctypes.POINTER(_POINTER)),
              *args, ctypes.byref(pointer))
        stack.callback(_release, pointer)
        return pointer

    def _device(self, id, stack):
        return self._object(self.enumerator, 5, "GetDevice", (ctypes.c_wchar_p,), (id,), stack)

    def _activate(self, device, interface_id, stack):
        interface_guid = GUID.from_string(interface_id)
        return self._object(device, 3, "Activate", (ctypes.POINTER(GUID), _UINT, _POINTER),
                            (ctypes.byref(interface_guid), 23, None), stack)

    def _string(self, pointer, index, operation):
        value = _POINTER()
        _call(pointer, index, operation, (ctypes.POINTER(_POINTER),), ctypes.byref(value))
        try:
            return ctypes.wstring_at(value.value) if value.value else ""
        finally:
            self.ole32.CoTaskMemFree(value)

    def _property(self, store, format_id, property_id):
        LOGGER.debug(f"Reading endpoint property {format_id}, {property_id}")
        key = PROPERTYKEY(GUID.from_string(format_id), property_id)
        variant = PROPVARIANT()
        _call(store, 5, "GetValue", (ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)),
              ctypes.byref(key), ctypes.byref(variant))
        try:
            if variant.type == 31 and variant.value.pointer:
                return ctypes.wstring_at(variant.value.pointer)
            if variant.type == 72 and variant.value.pointer:
                return str(ctypes.cast(variant.value.pointer, ctypes.POINTER(GUID)).contents)
            if variant.type == 19:
                return variant.value.unsigned
            return ""
        finally:
            self.ole32.PropVariantClear(ctypes.byref(variant))

    def _filters(self, device):
        filters = []
        with ExitStack() as stack:
            topology = self._activate(device, "2a07407e-6497-4a18-9787-32f79bd0d98f", stack)
            count = _UINT()
            _call(topology, 3, "GetConnectorCount", (ctypes.POINTER(_UINT),), ctypes.byref(count))
            for index in range(count.value):
                with ExitStack() as connector_stack:
                    try:
                        connector = self._object(topology, 4, "GetConnector", (_UINT,),
                                                 (index,), connector_stack)
                        id = self._string(connector, 10, "GetDeviceIdConnectedTo")
                        LOGGER.debug(f"Audio topology connector {index}: {id}")
                        if id.lower().startswith("{2}.\\\\?\\bth"):
                            filters.append(id)
                    except _WindowsError as error:
                        LOGGER.debug(f"Audio topology connector {index}: {error}")
        return filters

    def endpoints(self):
        endpoints = []
        with ExitStack() as stack:
            collection = self._object(self.enumerator, 3, "EnumAudioEndpoints", (_UINT, _UINT),
                                      (0, _ACTIVE | _UNPLUGGED), stack)
            count = _UINT()
            _call(collection, 3, "GetCount", (ctypes.POINTER(_UINT),), ctypes.byref(count))
            LOGGER.info(f"Found {count.value} active or unplugged render endpoints")
            for index in range(count.value):
                with ExitStack() as device_stack:
                    device = self._object(collection, 4, "Item", (_UINT,), (index,), device_stack)
                    LOGGER.debug(f"Inspecting render endpoint {index + 1} of {count.value}")
                    store = self._object(device, 4, "OpenPropertyStore", (_UINT,), (0,), device_stack)
                    state = _UINT()
                    _call(device, 6, "GetState", (ctypes.POINTER(_UINT),), ctypes.byref(state))
                    id = self._string(device, 5, "GetId")
                    LOGGER.debug(f"Inspecting endpoint {id}")
                    name = self._property(store, "a45c254e-df1c-4efd-8020-67d146a850e0", 14)
                    interface_name = self._property(store, "026e516e-b814-414b-83cd-856d6fef4822", 2)
                    container = self._property(store, "8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c", 2)
                    form_factor = self._property(store, "1da5d803-d492-4edd-8c23-e0c0ffee7f0e", 0)
                    try:
                        filters = self._filters(device)
                    except _WindowsError as error:
                        LOGGER.debug(f"Audio topology unavailable for {id}: {error}")
                        filters = []
                    LOGGER.debug(f"Endpoint id={id!r}, name={name!r}, interface={interface_name!r}, "
                                 f"container={container!r}, state={state.value}, "
                                 f"form_factor={form_factor!r}, bluetooth_filters={filters!r}")
                    endpoints.append(_Endpoint(id, name, interface_name, container, state.value, filters, form_factor))
        return endpoints

    def default_id(self, role=0):
        with ExitStack() as stack:
            try:
                device = self._object(self.enumerator, 4, "GetDefaultAudioEndpoint", (_UINT, _UINT),
                                      (0, role), stack)
            except _WindowsError as error:
                if error.code == _NOT_FOUND:
                    LOGGER.debug(f"No default render endpoint for role {role}")
                    return None
                raise
            id = self._string(device, 5, "GetId")
            LOGGER.debug(f"Default render endpoint for role {role}: {id}")
            return id

    def request_connect(self, filter_id):
        LOGGER.info(f"Requesting Bluetooth audio connection through {filter_id}")
        with ExitStack() as stack:
            device = self._device(filter_id, stack)
            control = self._activate(device, "28f54685-06fd-11d2-b27a-00a0c9223196", stack)
            prop = KSPROPERTY(GUID.from_string("7fa06c40-b8f6-4c7e-8556-e8c33a12e54d"), 0, 1)
            returned = _UINT()
            _call(control, 3, "KSPROPERTY_ONESHOT_RECONNECT",
                  (ctypes.POINTER(KSPROPERTY), _UINT, _POINTER, _UINT, ctypes.POINTER(_UINT)),
                  ctypes.byref(prop), ctypes.sizeof(prop), None, 0, ctypes.byref(returned))

    def set_default(self, id):
        LOGGER.info(f"Setting default output to {id} for all roles")
        with ExitStack() as stack:
            policy = self._create("870af99c-171d-4f9e-af0d-e63df40c2bc9",
                                  "f8679f50-850a-41cf-9c72-430f290290c8", stack)
            for role in range(3):
                _call(policy, 13, "SetDefaultEndpoint", (ctypes.c_wchar_p, _UINT), id, role)
        for attempt in range(10):
            if all(self.default_id(role) == id for role in range(3)):
                LOGGER.info(f"Verified default output {id} for all roles")
                return True
            time.sleep(0.2)
        LOGGER.warning(f"Could not verify default output {id} for all roles")
        return False


def _bluetooth_groups(endpoints):
    containers = {endpoint.container for endpoint in endpoints
                  if endpoint.container and endpoint.container != "00000000-0000-0000-0000-000000000000"
                  and endpoint.filters}
    groups = {container: [endpoint for endpoint in endpoints if endpoint.container == container]
              for container in containers}
    LOGGER.info(f"Found {len(groups)} Bluetooth audio device containers")
    return groups


class BluetoothDevice:
    def __init__(self, address, name, connected):
        self.id = address
        self.address = address
        self.name = name
        self.connected = connected

    @staticmethod
    def get_bluetooth_devices():
        with _AudioAPI() as api:
            groups = _bluetooth_groups(api.endpoints())
        devices = []
        for container, endpoints in groups.items():
            endpoint = endpoints[0]
            devices.append(BluetoothDevice(container, endpoint.interface_name or endpoint.name,
                                           any(item.state == _ACTIVE for item in endpoints)))
        return devices


class Sink:
    def __init__(self, id, name, default, address=None):
        self.id = id
        self.name = name
        self.default = default
        self.address = address

    @staticmethod
    def get_pipewire_sinks():
        with _AudioAPI() as api:
            endpoints = api.endpoints()
            default_id = api.default_id()
        bluetooth = _bluetooth_groups(endpoints)
        return [Sink(endpoint.id, endpoint.name, endpoint.id == default_id,
                     endpoint.container if endpoint.container in bluetooth else None)
                for endpoint in endpoints if endpoint.state == _ACTIVE]

    @staticmethod
    def get_default_pipewire_sink():
        return next((sink for sink in Sink.get_pipewire_sinks() if sink.default), None)

    @staticmethod
    def set_sink(label, retry=1, timeout=3.0, reconnect=False):
        LOGGER.info(f"Selecting output {label} with up to {retry} attempts")
        with _AudioAPI() as api:
            endpoints = api.endpoints()
            groups = _bluetooth_groups(endpoints)
            if label in groups:
                for attempt in range(retry):
                    LOGGER.debug(f"Bluetooth connection attempt {attempt + 1} of {retry}")
                    candidates = [endpoint for endpoint in groups[label] if endpoint.state == _ACTIVE]
                    if not candidates:
                        requested = False
                        for filter_id in {filter_id for endpoint in groups[label] for filter_id in endpoint.filters}:
                            try:
                                api.request_connect(filter_id)
                                requested = True
                            except _WindowsError as error:
                                LOGGER.warning(str(error))
                        if not requested:
                            LOGGER.warning(f"No Bluetooth reconnect request succeeded for {label}")
                            continue
                        deadline = time.monotonic() + max(timeout, 20.0)
                        while time.monotonic() < deadline:
                            time.sleep(0.5)
                            endpoints = api.endpoints()
                            candidates = [endpoint for endpoint in endpoints
                                          if endpoint.container == label and endpoint.state == _ACTIVE]
                            if candidates:
                                LOGGER.info(f"Bluetooth output became active for {label}")
                                break
                        if not candidates:
                            LOGGER.warning(f"Bluetooth output did not become active for {label}")
                    if candidates:
                        endpoint = min(candidates, key=lambda item: (item.form_factor != 3, item.id))
                        if api.set_default(endpoint.id):
                            return Sink(endpoint.id, endpoint.name, True, label)
                        return None
                return None
            endpoint = next((item for item in endpoints if item.id == label and item.state == _ACTIVE), None)
            if endpoint and api.set_default(endpoint.id):
                return Sink(endpoint.id, endpoint.name, True)
            LOGGER.warning(f"No active output found or default selection failed for {label}")
            return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect and switch Windows audio outputs")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("-l", "--list", action="store_true", help="list outputs and paired Bluetooth devices")
    actions.add_argument("-j", "--json", action="store_true", help="print devices as JSON")
    actions.add_argument("-s", "--sink", metavar="ID_OR_NAME", help="set an output by ID or exact name")
    parser.add_argument("-r", "--retry", type=int, default=1, help="Bluetooth connection attempts (default: 1)")
    parser.add_argument("--log", choices=("DEBUG", "INFO", "WARN", "ERROR"), default="WARN")
    args = parser.parse_args(argv)
    if args.retry < 1:
        parser.error("--retry must be at least 1")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s\t%(message)s"))
    old_level, old_propagate = LOGGER.level, LOGGER.propagate
    LOGGER.addHandler(handler)
    LOGGER.setLevel(getattr(logging, args.log))
    LOGGER.propagate = False
    try:
        if args.sink:
            devices = Sink.get_pipewire_sinks() + BluetoothDevice.get_bluetooth_devices()
            matches = [device for device in devices if str(device.id).casefold() == args.sink.casefold()]
            if not matches:
                matches = [device for device in devices if device.name.casefold() == args.sink.casefold()]
            if len(matches) != 1:
                LOGGER.error(f"Expected one output matching {args.sink!r}, found {len(matches)}; use an ID")
                return 1
            sink = Sink.set_sink(str(matches[0].id), retry=args.retry)
            if not sink:
                LOGGER.error(f"Could not set output {args.sink}")
                return 1
            print(f"Default output: {sink.name} ({sink.id})")
        else:
            sinks = sorted(Sink.get_pipewire_sinks(), key=lambda sink: sink.name)
            bluetooth = sorted(BluetoothDevice.get_bluetooth_devices(), key=lambda device: device.name)
            data = {
                "sinks": [{"id": sink.id, "name": sink.name, "address": sink.address,
                           "default": sink.default} for sink in sinks],
                "bluetooth": [{"id": device.id, "name": device.name, "connected": device.connected}
                              for device in bluetooth],
            }
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print("Windows audio outputs:")
                for sink in data["sinks"]:
                    print(f"  {sink['name']} | {sink['id']} | default: {sink['default']}")
                print("\nPaired Bluetooth audio devices:")
                for device in data["bluetooth"]:
                    print(f"  {device['name']} | {device['id']} | connected: {device['connected']}")
        return 0
    except (OSError, RuntimeError) as error:
        LOGGER.error(str(error))
        return 1
    finally:
        LOGGER.removeHandler(handler)
        handler.close()
        LOGGER.setLevel(old_level)
        LOGGER.propagate = old_propagate


if __name__ == "__main__":
    sys.exit(main())