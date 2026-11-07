import os
import ctypes
import io
import json
import sys
import unittest
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script.pwsink"))
from resources.lib import winsink


GUID = "12345678-1234-1234-1234-123456789abc"
AudioAPI = winsink._AudioAPI
class WindowsBackendTests(unittest.TestCase):

    def test_native_structures_match_windows_abi(self):
        self.assertEqual(str(winsink.GUID.from_string(GUID)), GUID)
        self.assertEqual(ctypes.sizeof(winsink.GUID), 16)
        self.assertEqual(ctypes.sizeof(winsink.PROPERTYKEY), 20)
        self.assertEqual(ctypes.sizeof(winsink.KSPROPERTY), 24)
        self.assertEqual(ctypes.sizeof(winsink.PROPVARIANT), 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)

    def setUp(self):
        self.wired = winsink._Endpoint("wired", "Speakers", "USB Audio", "wired-container", 1, [])
        self.bluetooth = winsink._Endpoint("bt", "Headphones", "Headphones", GUID, 8, ["filter"])
        self.connected = winsink._Endpoint("bt", "Headphones", "Headphones", GUID, 1, ["filter"])
        self.api = MagicMock()
        self.context = patch.object(winsink, "_AudioAPI")
        factory = self.context.start()
        factory.return_value.__enter__.return_value = self.api
        self.addCleanup(self.context.stop)

    def test_discovers_active_render_devices(self):
        self.api.endpoints.return_value = [self.wired, self.bluetooth]
        self.api.default_id.return_value = "wired"
        sinks = winsink.Sink.get_pipewire_sinks()
        self.assertEqual([(sink.id, sink.name, sink.default, sink.address) for sink in sinks],
                         [("wired", "Speakers", True, None)])

    def test_discovers_paired_bluetooth_devices(self):
        self.api.endpoints.return_value = [self.wired, self.bluetooth]
        devices = winsink.BluetoothDevice.get_bluetooth_devices()
        self.assertEqual((devices[0].id, devices[0].name, devices[0].connected),
                         (GUID, "Headphones", False))

    def test_connected_bluetooth_sink_uses_container_id(self):
        self.api.endpoints.return_value = [self.connected]
        self.api.default_id.return_value = "bt"
        sink = winsink.Sink.get_default_pipewire_sink()
        self.assertEqual((sink.id, sink.address, sink.default), ("bt", GUID, True))

    def test_switches_exact_wired_id(self):
        self.api.endpoints.return_value = [self.wired]
        self.api.set_default.return_value = True
        sink = winsink.Sink.set_sink("wired")
        self.assertEqual(sink.id, "wired")
        self.api.set_default.assert_called_once_with("wired")

    def test_reconnects_bluetooth_before_switching(self):
        self.api.endpoints.side_effect = [[self.bluetooth], [self.connected]]
        self.api.set_default.return_value = True
        with patch.object(winsink.time, "sleep"):
            sink = winsink.Sink.set_sink(GUID)
        self.assertEqual((sink.id, sink.address), ("bt", GUID))
        self.api.request_connect.assert_called_once_with("filter")
        self.api.set_default.assert_called_once_with("bt")

    def test_prefers_headphones_over_headset_endpoint(self):
        headset = winsink._Endpoint("a", "Headset", "Headphones", GUID, 1, ["filter"], 5)
        headphones = winsink._Endpoint("z", "Headphones", "Headphones", GUID, 1, ["filter"], 3)
        self.api.endpoints.return_value = [headset, headphones]
        self.api.set_default.return_value = True
        self.assertEqual(winsink.Sink.set_sink(GUID).id, "z")
        self.api.request_connect.assert_not_called()

    def test_failed_connection_request_retries(self):
        self.api.endpoints.return_value = [self.bluetooth]
        self.api.request_connect.side_effect = winsink._WindowsError("request", -1)
        self.assertIsNone(winsink.Sink.set_sink(GUID, retry=2))
        self.assertEqual(self.api.request_connect.call_count, 2)
        self.api.set_default.assert_not_called()

    def test_accepted_connection_request_can_still_time_out(self):
        self.api.endpoints.return_value = [self.bluetooth]
        with patch.object(winsink.time, "monotonic", side_effect=[0, 21]), \
             patch.object(winsink.time, "sleep"):
            self.assertIsNone(winsink.Sink.set_sink(GUID))
        self.api.request_connect.assert_called_once_with("filter")
        self.api.set_default.assert_not_called()

    def test_zero_container_is_not_a_bluetooth_device(self):
        self.bluetooth.container = "00000000-0000-0000-0000-000000000000"
        self.api.endpoints.return_value = [self.bluetooth]
        self.assertEqual(winsink.BluetoothDevice.get_bluetooth_devices(), [])

    def test_failed_wired_switch_is_not_reported_as_success(self):
        self.api.endpoints.return_value = [self.wired]
        self.api.set_default.return_value = False
        self.assertIsNone(winsink.Sink.set_sink("wired"))

    def test_default_must_match_all_roles(self):
        api = AudioAPI()
        api.default_id = MagicMock(side_effect=["wired", "wired", "other"] * 10)
        with patch.object(api, "_create", return_value=ctypes.c_void_p(1)), \
             patch.object(winsink, "_call") as call, patch.object(winsink.time, "sleep"):
            self.assertFalse(api.set_default("wired"))
        self.assertEqual([entry.args[-1] for entry in call.call_args_list], [0, 1, 2])

    def test_native_vtable_call_and_hresult(self):
        callback_type = winsink._WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32)
        callback = callback_type(lambda pointer, value: -1 if value == 7 else 0)
        table = (ctypes.c_void_p * 4)()
        table[3] = ctypes.cast(callback, ctypes.c_void_p).value
        table_pointer = ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p))
        instance = ctypes.pointer(table_pointer)
        pointer = ctypes.cast(instance, ctypes.c_void_p)
        winsink._call(pointer, 3, "test", (ctypes.c_uint32,), 3)
        with self.assertRaises(winsink._WindowsError):
            winsink._call(pointer, 3, "test", (ctypes.c_uint32,), 7)

    def test_reconnect_request_uses_bluetooth_audio_property(self):
        api = AudioAPI()
        api._device = MagicMock(return_value=ctypes.c_void_p(1))
        api._activate = MagicMock(return_value=ctypes.c_void_p(2))
        captured = []

        def capture(pointer, index, operation, argtypes, *args):
            prop = ctypes.cast(args[0], ctypes.POINTER(winsink.KSPROPERTY)).contents
            captured.append((index, operation, str(prop.set), prop.id, prop.flags, args[1]))

        with patch.object(winsink, "_call", side_effect=capture):
            api.request_connect("filter")
        self.assertEqual(captured, [(3, "KSPROPERTY_ONESHOT_RECONNECT",
                                     "7fa06c40-b8f6-4c7e-8556-e8c33a12e54d", 0, 1, 24)])

    def test_cli_json_keeps_debug_logs_on_stderr(self):
        self.api.endpoints.return_value = [self.wired, self.bluetooth]
        self.api.default_id.return_value = "wired"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(winsink.main(["--json", "--log", "DEBUG"]), 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["sinks"][0]["id"], "wired")
        self.assertEqual(data["bluetooth"][0]["id"], GUID)
        self.assertIn("INFO", stderr.getvalue())

    def test_cli_selects_exact_name_and_passes_retry(self):
        self.api.endpoints.return_value = [self.wired]
        self.api.default_id.return_value = "wired"
        self.api.set_default.return_value = True
        with redirect_stdout(io.StringIO()), patch.object(winsink.Sink, "set_sink", wraps=winsink.Sink.set_sink) as select:
            self.assertEqual(winsink.main(["--sink", "speakers", "--retry", "2"]), 0)
        select.assert_called_once_with("wired", retry=2)

    def test_cli_rejects_ambiguous_names(self):
        duplicate = winsink._Endpoint("other", "Speakers", "HDMI", "other", 1, [])
        self.api.endpoints.return_value = [self.wired, duplicate]
        self.api.default_id.return_value = "wired"
        with redirect_stderr(io.StringIO()), patch.object(winsink.Sink, "set_sink") as select:
            self.assertEqual(winsink.main(["--sink", "Speakers"]), 1)
        select.assert_not_called()

    def test_cli_reports_discovery_errors(self):
        self.api.endpoints.side_effect = RuntimeError("discovery failed")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(winsink.main(["--log", "DEBUG"]), 1)
        self.assertIn("discovery failed", stderr.getvalue())


class KodiSelectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dialog = MagicMock()
        cls.dialog.select.return_value = -1
        gui = MagicMock()
        gui.Dialog.return_value = cls.dialog
        modules = {name: MagicMock() for name in ("xbmc", "xbmcaddon", "xbmcvfs")}
        modules["xbmcgui"] = gui
        modules["xbmcvfs"].translatePath.return_value = "addon"
        modules["xbmcaddon"].Addon.return_value.getLocalizedString.return_value = "label"
        path = os.path.join(os.path.dirname(__file__), "..", "script.pwsink", "addon.py")
        spec = importlib.util.spec_from_file_location("test_kodi_addon", path)
        cls.addon_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.object(sys, "platform", "win32"):
            spec.loader.exec_module(cls.addon_module)

    def test_select_handles_missing_default(self):
        addon = self.addon_module
        setting = addon.Setting(GUID, "Headphones", "", 4, False)
        with patch.object(sys, "platform", "win32"), \
             patch.object(addon.Setting, "get_settings", return_value=[setting]), \
             patch.object(winsink.Sink, "get_default_pipewire_sink", return_value=None), \
             patch.object(winsink.BluetoothDevice, "get_bluetooth_devices", return_value=[]):
            addon.select()
        self.assertEqual(self.dialog.select.call_args.kwargs["preselect"], -1)

    def test_select_marks_connected_bluetooth_default(self):
        addon = self.addon_module
        setting = addon.Setting(GUID, "Headphones", "", 4, False)
        device = winsink.BluetoothDevice(GUID, "Headphones", True)
        default = winsink.Sink("endpoint", "Speakers (Headphones)", True, GUID)
        with patch.object(sys, "platform", "win32"), \
             patch.object(addon.Setting, "get_settings", return_value=[setting]), \
             patch.object(winsink.Sink, "get_default_pipewire_sink", return_value=default), \
             patch.object(winsink.BluetoothDevice, "get_bluetooth_devices", return_value=[device]):
            addon.select()
        self.assertEqual(self.dialog.select.call_args.kwargs["preselect"], 0)
        self.assertEqual(len(self.dialog.select.call_args.args[1]), 1)


if __name__ == "__main__":
    unittest.main()