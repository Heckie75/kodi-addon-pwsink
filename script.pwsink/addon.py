import json
import os
import re
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from resources.lib import pwsink

addon = xbmcaddon.Addon()
addon_dir = xbmcvfs.translatePath(addon.getAddonInfo('path'))

_ICON_DEFAULT = 14
_ICON_DISCONNECT = 15


class XbmcLogger(pwsink.MyLogger):

    def log(self, level, s):

        if level >= self.level:
            xbmc.log(s, xbmc.LOGINFO)


class Setting():

    _MAX_SINKS = 10

    def __init__(self, address: str, name: str, alias: str, icon: int, hidden: bool):

        self.address = address
        self.name = name
        self.alias = alias
        self.icon = icon
        self.hidden = hidden

    def is_bluetooth(self) -> bool:

        return re.match(r"^[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}$", self.address)

    @staticmethod
    def get_settings() -> 'list[Setting]':

        settings: 'list[Setting]' = list()

        for i in range(Setting._MAX_SINKS):
            setting = Setting.get_setting(i)
            if setting:
                settings.append(setting)

        settings.sort(key=lambda s: s.alias or s.name)
        return settings

    @staticmethod
    def get_setting(i: int) -> 'Setting | None':

        address = addon.getSettingString(f"address_{i}")
        if not address:
            return None

        return Setting(address=address, name=addon.getSettingString(f"name_{i}"), alias=addon.getSettingString(
            f"alias_{i}"), icon=addon.getSettingInt(f"icon_{i}"), hidden=addon.getSettingBool(f"hide_{i}"))

    @staticmethod
    def reset() -> None:

        for i in range(Setting._MAX_SINKS):

            addon.setSettingString(f"name_{i}", "")
            addon.setSettingString(f"address_{i}", "")
            addon.setSettingString(f"alias_{i}", "")
            addon.setSettingInt(f"icon_{i}", 0)
            addon.setSettingBool(f"hide_{i}", False)

    @staticmethod
    def refresh() -> None:

        def get_device_icon(name: str, is_bluetooth=False) -> str:

            if is_bluetooth:
                return 4

            elif "hdmi" in name.lower():
                return 1

            elif "displayport" in name.lower():
                return 2

            elif "usb" in name.lower():
                return 3

            else:
                return 0

        sinks = [sink for sink in pwsink.Sink.get_pipewire_sinks()
                 if not sink.address]
        sinks.extend(pwsink.BluetoothDevice.get_bluetooth_devices())
        sinks.sort(key=lambda s: s.name)

        former_settings = Setting.get_settings()
        Setting.reset()

        for i, sink in enumerate(sinks):

            addon.setSettingString(f"name_{i}", sink.name)
            addon.setSettingString(f"address_{i}", str(sink.id))

            former_setting = [
                setting for setting in former_settings if setting.address == str(sink.id)]
            if former_setting:
                addon.setSettingString(f"alias_{i}", former_setting[0].alias)
                addon.setSettingInt(f"icon_{i}", former_setting[0].icon)
                addon.setSettingBool(f"hide_{i}", former_setting[0].hidden)

            else:
                addon.setSettingInt(f"icon_{i}", get_device_icon(
                    sink.name, is_bluetooth=type(sink) == pwsink.BluetoothDevice))


def get_icon(id: int, active: bool = False, connected: bool = False) -> str:

    icon = ["icon_analog", "icon_hdmi", "icon_dp", "icon_usb", "icon_bluetooth", "icon_stereo", "icon_speaker", "icon_headphones",
            "icon_livingroom", "icon_bedroom", "icon_kitchen", "icon_bathroom", "icon_hall", "icon_combine", "icon_default", "icon_disconnect"][id]

    if active:
        icon = f"{icon}_active"

    elif connected:
        icon = f"{icon}_connected"

    return os.path.join(
        addon_dir, "resources", "assets", f"{icon}.png")


def set_sink(setting: Setting) -> None:

    sink = pwsink.Sink.set_sink(
        setting.address, retry=addon.getSettingInt("retries"), reconnect=addon.getSettingBool("reconnect"))
    if sink:
        xbmcgui.Dialog().notification(heading=addon.getLocalizedString(32003),
                                      message=addon.getLocalizedString(32004) % (setting.alias or setting.name), icon=get_icon(setting.icon, active=True))

    else:
        xbmcgui.Dialog().notification(heading=addon.getLocalizedString(32015),
                                      message=addon.getLocalizedString(32016) % (setting.alias or setting.name), icon=get_icon(setting.icon))


def select():

    def _get_options() -> 'tuple[list[Setting], list[xbmcgui.ListItem], int, bool]':

        preselect = -1
        disconnect = None
        listitems = list()
        default_sink = pwsink.Sink.get_default_pipewire_sink()
        connected_bt_devices = [
            d.address for d in pwsink.BluetoothDevice.get_bluetooth_devices() if d.connected]

        settings = [s for s in Setting.get_settings() if not s.hidden]
        for i, setting in enumerate(settings):

            listitem = xbmcgui.ListItem()
            label = setting.alias or setting.name

            label2 = list()
            is_active = setting.name == default_sink.name
            if is_active:
                listitem.setProperty("preselect", "true")
                preselect = i
                label2.append(addon.getLocalizedString(32057))

            is_connected = False
            if setting.is_bluetooth() and setting.address in connected_bt_devices:
                is_connected = True
                disconnect = label
                label2.append(addon.getLocalizedString(32017))

            listitem.setLabel(label)
            if label2:
                listitem.setLabel2(", ".join(label2))

            listitem.setArt(
                {"thumb": get_icon(id=setting.icon, active=is_active, connected=is_connected)})

            listitems.append(listitem)

        if disconnect:
            listitem = xbmcgui.ListItem(
                label=addon.getLocalizedString(32018), label2=disconnect)
            listitem.setArt({"thumb": get_icon(id=_ICON_DISCONNECT)})
            listitems.append(listitem)

        return settings, listitems, preselect, disconnect

    settings, listitems, preselect, disconnect = _get_options()
    if not settings:
        addon.openSettings()
        return

    selected = xbmcgui.Dialog().select(
        addon.getLocalizedString(32002), listitems, preselect=preselect, useDetails=True)

    if disconnect and selected == len(listitems) - 1:
        pwsink.BluetoothDevice.disconnect()
        xbmcgui.Dialog().notification(heading=addon.getLocalizedString(32001),
                                      message=addon.getLocalizedString(
                                          32019) % disconnect,
                                      icon=get_icon(_ICON_DISCONNECT))

    elif selected >= 0:
        set_sink(settings[selected])


def add_to_favourites(id: int) -> None:

    name = addon.getSettingString(f"name_{id}")
    alias = addon.getSettingString(f"alias_{id}")
    icon = addon.getSettingInt(f"icon_{id}")
    thumbnail = get_icon(icon)

    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "Favourites.AddFavourite",
        "params": {
            "title": alias or name,
            "type": "media",
            "path": f"plugin://script.pwsink/?id={id}",
            "thumbnail": thumbnail
        }
    }))
    xbmcgui.Dialog().notification(heading=addon.getLocalizedString(32001),
                                  message=addon.getLocalizedString(
        32020),
        icon=thumbnail)


if __name__ == "__main__":

    pwsink.LOGGER = XbmcLogger(xbmc.LOGDEBUG)

    args = sys.argv
    if len(args) == 2 and args[1] == "discover":
        Setting.refresh()

    elif len(args) == 3 and args[1] == "add_fav":
        add_to_favourites(id=int(args[2]))

    elif len(args) == 4 and args[2].startswith("?id"):
        m = re.match(r"^\?id=([0-9]+)$", args[2])
        if m:
            setting = Setting.get_setting(int(m.groups()[0]))
            set_sink(setting)

    else:
        select()
