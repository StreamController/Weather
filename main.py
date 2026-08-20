import json
from GtkHelper.GtkHelper import ComboRow
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

# Import gtk modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

import sys
import os
from PIL import Image
from loguru import logger as log
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # urllib3 module layout can differ across versions
    Retry = None
from threading import Timer


# open-meteo is fetched on a background timer; without an explicit timeout a
# stalled read hangs the refresh thread indefinitely and the key freezes on
# whatever it last showed. A shared session with connect/read timeouts and
# bounded retries makes a bad fetch fail within seconds so the retry timer
# can take over.
WEATHER_HTTP_TIMEOUT = (5, 10)  # (connect, read) seconds


def _build_session() -> requests.Session:
    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


SESSION = _build_session()


# Add plugin to sys.paths
sys.path.append(os.path.dirname(__file__))

# Import globals
import globals as gl

# Import own modules
from src.backend.DeckManagement.DeckController import DeckController
from src.backend.PageManagement.Page import Page

class WindDirection(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.show_interval = 30 # minutes
        self.retry_interval = 2 # minutes, after a failed fetch
        # Keep showing the last good reading through a run of transient
        # failures; only fall back to the error icon after this many
        # consecutive misses (or if there has never been a good reading).
        self.max_failures_before_error = 3

        self.show_timer: Timer = None
        self._last_wind_data = None
        self._consecutive_failures = 0

    def on_ready(self):
        self.show()

    def get_config_rows(self) -> list:
        self.units_model = Gtk.ListStore.new([str, int])
        self.units_row = ComboRow(title=self.plugin_base.lm.get("actions.unit.title"), model=self.units_model)
        self.lat_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.lat-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)
        self.lon_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.long-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)

        self.units_cell_renderer = Gtk.CellRendererText()
        self.units_row.combo_box.pack_start(self.units_cell_renderer, True)
        self.units_row.combo_box.add_attribute(self.units_cell_renderer, "text", 0)

        self.load_units_model()
        self.load_config_defaults()

        # Connect signals
        self.lat_entry.connect("notify::text", self.on_lat_changed)
        self.lon_entry.connect("notify::text", self.on_lon_changed)
        self.units_row.combo_box.connect("changed", self.on_units_changed)

        return [self.lat_entry, self.lon_entry, self.units_row]
    
    def load_units_model(self):
        self.units_model.append([self.plugin_base.lm.get("actions.units.metric"), 1])
        self.units_model.append([self.plugin_base.lm.get("actions.units.imperial"), 2])
    
    def on_lat_changed(self, entry, text):
        settings = self.get_settings()
        settings["lat"] = entry.get_text()
        self.set_settings(settings)

        self.show()
    
    def on_lon_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lon"] = entry.get_text()
        self.set_settings(settings)

        self.show()

    def on_units_changed(self, combo_box, *args):
        unit = self.units_model[combo_box.get_active()][1]

        settings = self.get_settings()
        settings["unit"] = unit
        self.set_settings(settings)

        self.show()


    def load_config_defaults(self):
        settings = self.get_settings()
        self.lat_entry.set_text(settings.get("lat", "")) # Does not accept None
        self.lon_entry.set_text(settings.get("lon", "")) # Does not accept None

        if settings.get("unit") == 2: #Imperial
            self.units_row.combo_box.set_active(1)
        else: #Celcius and none
            self.units_row.combo_box.set_active(0)



    def get_wind_data(self) -> list[float]:
        settings = self.get_settings()
        lat = settings.get("lat")
        lon = settings.get("lon")
        imperial = settings.get("unit") == 2

        # Try to convert lat and lon to float]
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            lat = None
            lon = None

        if lat is None or lon is None:
            return
        
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["wind_speed_10m", "wind_direction_10m"]
        }

        if imperial:
            params["wind_speed_unit"] = "mph"

        # Make request with the custom cache session
        try:
            resp = SESSION.get(url, params=params, timeout=WEATHER_HTTP_TIMEOUT)
        except Exception as e:
            log.error(e)
            return


        # Parse response. A gateway error (e.g. a 502 HTML page) used to raise
        # out of resp.json() uncaught; treat any parse/shape problem as no data.
        try:
            data = resp.json()
            return [
                data["current"]["wind_direction_10m"],
                data["current"]["wind_speed_10m"],
                data["current_units"]["wind_speed_10m"]
            ]
        except Exception as e:
            log.error(f"Weather response parse failed (status {resp.status_code}): {e}")
            return
    
    def show(self):
        # Stop timer if active
        if self.show_timer is not None:
            if self.show_timer.is_alive:
                self.show_timer.cancel()

        wind_data = self.get_wind_data()
        if wind_data is None:
            # Tolerate transient failures: re-show the last good reading and
            # only surface the error icon after several misses in a row.
            self._consecutive_failures += 1
            if (self._last_wind_data is not None
                    and self._consecutive_failures < self.max_failures_before_error):
                self._render_wind(self._last_wind_data)
            else:
                self.show_error()
            # Re-arm the timer on failure too, so a transient network/DNS
            # blip self-heals instead of freezing the error icon until the
            # next key press (the early return used to skip the reschedule).
            self.show_timer = Timer(self.retry_interval*60, self.show)
            self.show_timer.start()
            return

        self._consecutive_failures = 0
        self._last_wind_data = wind_data
        self._render_wind(wind_data)

        # Launch timer
        self.show_timer = Timer(self.show_interval*60, self.show)
        self.show_timer.start()

    def _render_wind(self, wind_data):
        wind_direction, wind_speed, wind_speed_unit = wind_data

        self.set_bottom_label(f"{int(wind_speed)} {wind_speed_unit}", font_size=12)

        with Image.open(os.path.join(self.plugin_base.PATH, "assets", "weather-icons", "wind_direction.png")) as img:
            image = img.copy()

        image = image.rotate(wind_direction, expand=True)

        self.set_media(image=image, size=0.85, valign=-1)

    def on_key_down(self):
        # Force update
        self.show()
    
    def get_custom_config_area(self):
        return Gtk.Label(label=self.plugin_base.lm.get("actions.open-meteo-thanks"))


class Weather(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.show_interval = 30 # minutes
        self.retry_interval = 2 # minutes, after a failed fetch
        # Keep showing the last good reading through a run of transient
        # failures; only fall back to the error icon after this many
        # consecutive misses (or if there has never been a good reading).
        self.max_failures_before_error = 3

        self.show_timer: Timer = None
        self._last_weather = None
        self._consecutive_failures = 0
        
    def on_ready(self):
        self.show()

    def on_key_down(self):
        # Force update
        self.show()

    def get_config_rows(self) -> list:
        self.units_model = Gtk.ListStore.new([str, int])
        self.units_row = ComboRow(title=self.plugin_base.lm.get("actions.unit.title"), model=self.units_model)
        self.lat_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.lat-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)
        self.lon_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.long-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)

        self.units_cell_renderer = Gtk.CellRendererText()
        self.units_row.combo_box.pack_start(self.units_cell_renderer, True)
        self.units_row.combo_box.add_attribute(self.units_cell_renderer, "text", 0)

        self.load_units_model()
        self.load_config_defaults()

        # Connect signals
        self.lat_entry.connect("notify::text", self.on_lat_changed)
        self.lon_entry.connect("notify::text", self.on_lon_changed)
        self.units_row.combo_box.connect("changed", self.on_units_changed)

        return [self.lat_entry, self.lon_entry, self.units_row]
    
    def load_units_model(self):
        self.units_model.append([self.plugin_base.lm.get("actions.units.celsius"), 1])
        self.units_model.append([self.plugin_base.lm.get("actions.units.fahrenheit"), 2])
    
    def get_custom_config_area(self):
        return Gtk.Label(label=self.plugin_base.lm.get("actions.open-meteo-thanks"))
    
    def on_lat_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lat"] = entry.get_text()
        self.set_settings(settings)

        self.show()
    
    def on_lon_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lon"] = entry.get_text()
        self.set_settings(settings)

        self.show()

    def on_units_changed(self, combo_box, *args):
        unit = self.units_model[combo_box.get_active()][1]

        settings = self.get_settings()
        settings["unit"] = unit
        self.set_settings(settings)

        self.show()

    def load_config_defaults(self):
        settings = self.get_settings()
        self.lat_entry.set_text(settings.get("lat", "")) # Does not accept None
        self.lon_entry.set_text(settings.get("lon", "")) # Does not accept Non

        if settings.get("unit") == 2: #Imperial
            self.units_row.combo_box.set_active(1)
        else: #Celcius and none
            self.units_row.combo_box.set_active(0)


    def show(self):
        # Stop timer if active
        if self.show_timer is not None:
            if self.show_timer.is_alive:
                self.show_timer.cancel()

        weather = self.get_weather()

        if weather is None:
            # Tolerate transient failures: re-show the last good reading and
            # only surface the error icon after several misses in a row.
            self._consecutive_failures += 1
            if (self._last_weather is not None
                    and self._consecutive_failures < self.max_failures_before_error):
                self._render_weather(self._last_weather)
            else:
                self.show_error()
            # Re-arm the timer on failure too, so a transient network/DNS
            # blip self-heals instead of freezing the error icon until the
            # next key press (the early return used to skip the reschedule).
            self.show_timer = Timer(self.retry_interval*60, self.show)
            self.show_timer.start()
            return

        self._consecutive_failures = 0
        self._last_weather = weather
        self._render_weather(weather)

        # Launch timer
        self.show_timer = Timer(self.show_interval*60, self.show)
        self.show_timer.start()

    def _render_weather(self, weather):
        weather_code, is_day, temperature, temperature_unit = weather

        image_to_show = self.get_image_to_show(weather_code=weather_code, night=not is_day)
        media_path = os.path.join(self.plugin_base.PATH, "assets", "weather-icons", f"{image_to_show}.png")

        self.set_media(media_path=media_path, size=0.8, valign=-1)

        self.set_bottom_label(f"{int(temperature)} {temperature_unit}", font_size=12)

    def get_weather(self) -> int:
        settings = self.get_settings()
        lat = settings.get("lat")
        lon = settings.get("lon")

        imperial = settings.get("unit") == 2

        # Try to convert lat and lon to float]
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            lat = None
            lon = None

        if lat is None or lon is None:
            return
        
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["weather_code", "is_day", "temperature_2m"]
        }

        if imperial:
            params["temperature_unit"] = "fahrenheit"

        # Make request with the custom cache session
        try:
            resp = SESSION.get(url, params=params, timeout=WEATHER_HTTP_TIMEOUT)
        except Exception as e:
            log.error(e)
            return


        # Parse response. A gateway error (e.g. a 502 HTML page) used to raise
        # out of resp.json() uncaught; treat any parse/shape problem as no data.
        try:
            data = resp.json()
            return [data["current"]["weather_code"], data["current"]["is_day"], data["current"]["temperature_2m"], data["current_units"]["temperature_2m"]]
        except Exception as e:
            log.error(f"Weather response parse failed (status {resp.status_code}): {e}")
            return

    def get_image_to_show(self, weather_code: int, night: bool) -> str:
        wc = weather_code
        if wc == 0:
            # Clear sky
            if night:
                return "clear_night"
            else:
                return "sunny"
        elif wc in range(1, 4):
            # Partly cloudy
            if night:
                return "cloudy_night"
            else:
                return "cloud"
        elif wc in range(45, 49):
            # Fog
            return "foggy"
        elif wc in range(51, 58):
            # Drizzle
            return "rainy_light"
        elif wc in range(61, 68) or wc in range(80, 87):
            # Rain
            return "rainy_heavy"
        elif wc in range(71, 78):
            # Snow
            return "snowy"
        elif wc in range(95, 100):
            # Thunderstorm
            return "thunderstorm"
        


class WeatherPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.init_locale_manager()

        self.lm = self.locale_manager

        ## Register actions
        self.wind_direction_holder = ActionHolder(
            plugin_base=self,
            action_base=WindDirection,
            action_id_suffix="WindDirection",
            action_name=self.lm.get("actions.wind-direction.name"),
            icon=Gtk.Image(icon_name="weather-windy-symbolic"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED
            }
        )
        self.add_action_holder(self.wind_direction_holder)

        self.weather_holder = ActionHolder(
            plugin_base=self,
            action_base=Weather,
            action_id_suffix="Weather",
            action_name=self.lm.get("actions.weather.name"),
            icon=Gtk.Image(icon_name="weather-clear-symbolic"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED
            }
        )
        self.add_action_holder(self.weather_holder)

        # Register plugin
        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/StreamController/Weather",
            plugin_version="1.0.0",
            app_version="1.0.0-alpha"
        )

    def init_locale_manager(self):
        self.lm = self.locale_manager
        self.lm.set_to_os_default()

    def get_selector_icon(self) -> Gtk.Widget:
        return Gtk.Image(icon_name="weather-clear-symbolic")