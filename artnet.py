"""
Home Assistant support for Art-Net/DMX lights over IP
"""
import asyncio
import logging
import socket
from struct import pack

from homeassistant.const import (CONF_DEVICES, CONF_HOST, CONF_NAME, CONF_PORT, CONF_TYPE)
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_TRANSITION,
                                            Light, PLATFORM_SCHEMA, SUPPORT_BRIGHTNESS,
                                            SUPPORT_RGB_COLOR, SUPPORT_TRANSITION)
import homeassistant.helpers.config_validation as cv
import homeassistant.util.color as color_util
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

DATA_ARTNET = 'light_artnet'

CONF_CHANNEL = 'channel'
CONF_DMX_CHANNELS = 'dmx_channels'
CONF_DEFAULT_COLOR = 'default_rgb'
CONF_DEFAULT_LEVEL = 'default_level'

# Light types
CONF_LIGHT_TYPE_DIMMER = 'dimmer'
CONF_LIGHT_TYPE_RGB = 'rgb'
CONF_LIGHT_TYPES = [CONF_LIGHT_TYPE_DIMMER, CONF_LIGHT_TYPE_RGB]

# Number of channels used by each light type
CHANNEL_COUNT_MAP, FEATURE_MAP, COLOR_MAP = {}, {}, {}
CHANNEL_COUNT_MAP[CONF_LIGHT_TYPE_DIMMER] = 1
CHANNEL_COUNT_MAP[CONF_LIGHT_TYPE_RGB] = 3

# Features supported by light types
FEATURE_MAP[CONF_LIGHT_TYPE_DIMMER] = (SUPPORT_BRIGHTNESS | SUPPORT_TRANSITION)
FEATURE_MAP[CONF_LIGHT_TYPE_RGB] = (SUPPORT_BRIGHTNESS | SUPPORT_TRANSITION | SUPPORT_RGB_COLOR)

# Default color for each light type if not specified in configuration
COLOR_MAP[CONF_LIGHT_TYPE_DIMMER] = None
COLOR_MAP[CONF_LIGHT_TYPE_RGB] = [255, 255, 255]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_DMX_CHANNELS): vol.All(vol.Coerce(int), vol.Range(min=1, max=512)),
    vol.Optional(CONF_PORT): cv.port,
    vol.Optional(CONF_DEFAULT_LEVEL): cv.byte,
    vol.Required(CONF_DEVICES): vol.All(cv.ensure_list, [
        {
            vol.Required(CONF_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=512)),
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_TYPE): vol.In(CONF_LIGHT_TYPES),
            vol.Optional(CONF_DEFAULT_LEVEL): cv.byte,
            vol.Optional(CONF_DEFAULT_COLOR): vol.All(vol.ExactSequence((cv.byte, cv.byte, cv.byte)),
                vol.Coerce(tuple)),
        }
    ]),
})

@asyncio.coroutine
def async_setup_platform(hass, config, add_devices, discovery_info=None):
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT, 6454)

    # Send the specified default level to pre-fill the channels with
    overall_default_level = config.get(CONF_DEFAULT_LEVEL)

    dmx = None
    if not dmx:
        dmx = DMXGateway(host, port, overall_default_level, config[CONF_DMX_CHANNELS])

    add_devices(ArtnetLight(light, dmx) for light in config[CONF_DEVICES])

class ArtnetLight(Light):
    """Representation of an Artnet Light."""

    def __init__(self, light, controller):
        """Initialize an artnet Light."""
        self._controller = controller
        self._channel = light[CONF_CHANNEL] # Move to using _channels
        self._type = light[CONF_TYPE]
        self._channel_count = int(CHANNEL_COUNT_MAP.get(self._type, 1))
        self._name = light['name']
        self._channels = [channel for channel in range(self._channel, self._channel + self._channel_count)]
        self._features = FEATURE_MAP.get(self._type)

        if CONF_DEFAULT_COLOR in light:
            tmpColor = light[CONF_DEFAULT_COLOR]
            scale = max(tmpColor)/255
            self._rgb = (int(tmpColor[0]/scale), 
                         int(tmpColor[1]/scale),
                         int(tmpColor[2]/scale))
            self._brightness = max(tmpColor)
        else:
            self._rgb = COLOR_MAP.get(self._type)

        # Synchronise state of channels (without sending any commands) with Artnet wrapper
        if self._type == CONF_LIGHT_TYPE_RGB:
            scaled_rgb = scale_rgb_to_brightness(self._rgb, self._brightness)
            self._controller.set_channel_rgb(self._channel, scaled_rgb, False)
        else:
            if CONF_DEFAULT_LEVEL in light:
                self._controller.set_channel(self._channel, light[CONF_DEFAULT_LEVEL], False)
            self._brightness = self._controller.get_channel_level(self._channel)

        self._state = self._brightness >= 0
        
    @property
    def name(self):
        """Return the display name of this light."""
        return self._name

    @property
    def brightness(self):
        """Return the brightness of the light."""
        return self._brightness

    @property
    def device_state_attributes(self):
        data = {}
        data['dmx_channels'] = self._channels
        return data

    @property
    def is_on(self):
        """Return true if light is on."""
        return self._state
    
    @property
    def rgb_color(self):
        """Return the RBG color value."""
        return self._rgb

    @property
    def supported_features(self):
        """Flag supported features."""
        return self._features

    @property
    def should_poll(self):
        return False

    @asyncio.coroutine
    def async_turn_on(self, **kwargs):
        """Instruct the light to turn on.
        Move to using one method on the DMX class to set/fade either a single channel or group of channels 
        """
        self._state = True
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]

        if ATTR_RGB_COLOR in kwargs:
            self._rgb = kwargs[ATTR_RGB_COLOR]

        if self._type == CONF_LIGHT_TYPE_RGB:
            scaled_rgb = scale_rgb_to_brightness(self._rgb, self._brightness)
            logging.debug('Setting light %s RGB to %s, scaled to %s', self._name, self._rgb, scaled_rgb)
            yield from self._controller.fade_channels(self._channels, scaled_rgb, int(kwargs.get(ATTR_TRANSITION, 0)))
        else:
            # Use fade channels for single channel fixtures
            yield from self._controller.fade_channels(self._channels, self._brightness, int(kwargs.get(ATTR_TRANSITION, 0)))

        self.async_schedule_update_ha_state()

    @asyncio.coroutine
    def async_turn_off(self, **kwargs):
        """Instruct the light to turn off."""
        yield from self._controller.fade_channels(self._channels, 0, kwargs.get(ATTR_TRANSITION, 0))
        self._state = False
        self.async_schedule_update_ha_state()

    def update(self):
        """Fetch update state."""
        # Nothing to return

class DMXGateway(object):
    """
    Class to keep track of the values of DMX channels and provide utilities to
    send values to the DMX gateway.
    """

    def __init__(self, host, port=6454, default_level=0, number_of_channels=512):
        """
        Initialise a bank of channels, with a default value specified by the caller.
        """

        self._host = host
        self._port = port

        # Ensure number of channels is within the universe
        if number_of_channels <= 512:
            self._number_of_channels = number_of_channels
        else:
            self._number_of_channels = 512

        # Number of channels must be even
        if number_of_channels % 2 != 0:
            self._number_of_channels += 1

        # Check the default value is 0-255
        if 0 <= default_level <= 255:
            self._default_level = default_level
            
        # Initialise the DMX channel array with the default values
        self._channels = [default_level] * self._number_of_channels

        # Initialise socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP

        packet = bytearray()
        packet.extend(map(ord, "Art-Net"))
        packet.append(0x00) # Null terminate Art-Net
        packet.extend([0x00, 0x50]) # Opcode ArtDMX 0x5000 (Little endian)
        packet.extend([0x00, 0x0e]) # Protocol version 14
        packet.extend([0x00, 0x00]) # Sequence, Physical
        packet.extend([0x00, 0x00]) # Universe
        packet.extend(pack('>h', self._number_of_channels)) # Pack the number of channels Big endian
        self._base_packet = packet

    def send(self):
        """
        Send the current state of DMX values to the gateway via UDP packet.
        """
        # Copy the base packet then add the channel array
        packet = self._base_packet[:]
        packet.extend(self._channels)
        self._socket.sendto(packet, (self._host, self._port))
        logging.debug("Sending Art-Net frame")

    @asyncio.coroutine
    def fade_channels(self, channels, value, seconds, fps=40):
        original_values = self._channels[:]
        # Minimum of one frame for a snap transition
        number_of_frames = max(int(seconds*fps),1)

        # Single value for standard channels, RGB channels will have 3 or more
        value_arr = [value]
        if type(value) is tuple or type(value) is list:
            value_arr = value

        for i in range(1, number_of_frames+1):
            values_changed = False

            for x, channel in enumerate(channels):
                target_value = value_arr[min(x, len(value_arr)-1)]
                increment = (target_value - original_values[channel-1])/(number_of_frames)

                next_value = int(round(original_values[channel-1] + (increment * i)))

                if self._channels[channel-1] != next_value:
                    self._channels[channel-1] = next_value
                    values_changed = True

            if values_changed:
                self.send()
            
            yield from asyncio.sleep(1./fps)

    def set_channel(self, channel, value, send_immediately=True):
        """
        Set a single DMX channel to the specified value. If send_immediately is specified as
        false, the changes will not be send to the gateway. This is useful to be able to cue
        up several changes at once.
        """
        if 1 <= channel <= self._number_of_channels and 0 <= value <= 255:
            logging.info('Setting channel %i to %i with send immediately = %s', int(channel),
                         int(value), send_immediately)
            self._channels[int(channel)-1] = value
            if send_immediately:
                self.send()
            return True
        else:
            return False

    def get_channel_level(self, channel):
        """
        Return the current value we have for the specified channel.
        """
        return self._channels[int(channel)-1]


    def set_channel_rgb(self, channel, values, send_immediately=True):
        for i in range(0, len(values)):
            logging.info('Setting channel %i to %i with send immediately = %s', channel+i, values[i], send_immediately)
            if (channel+i <= self._number_of_channels) and (0 <= values[i] <= 255):
                self._channels[channel-1+i] = values[i]

        if send_immediately is True:
            self.send()
        return True

    @property
    def default_level(self):
        return self._default_level

def scale_rgb_to_brightness(rgb, brightness):
    brightness_scale = (brightness / 255)
    scaled_rgb = [round(rgb[0] * brightness_scale),
                  round(rgb[1] * brightness_scale),
                  round(rgb[2] * brightness_scale)]
    return scaled_rgb