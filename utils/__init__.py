import aiohttp
import xmltodict
from dotmap import DotMap

from settings import settings
from datetime import timedelta, datetime


UPNP_AVT_SERVICE_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"
UPNP_RC_SERVICE_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"

# Some renderers implement newer versions of the same services - the Hegel H150,
# for example, advertises AVTransport:2 and RenderingControl:2. Their SOAP replies
# are namespaced with that versioned URN, so it has to be stripped here too:
# otherwise every "<Action>Response" key stays fully qualified and control() reads
# None back for every call that has a response.
UPNP_SERVICE_VERSIONS = (1, 2, 3, 4)
UPNP_SERVICE_BASES = (
    "urn:schemas-upnp-org:service:AVTransport",
    "urn:schemas-upnp-org:service:RenderingControl",
    "urn:schemas-upnp-org:service:ConnectionManager",
)
UPNP_VERSIONED_NAMESPACES = {
    f"{base}:{version}": None
    for base in UPNP_SERVICE_BASES
    for version in UPNP_SERVICE_VERSIONS
}


def same_service(a: str, b: str) -> bool:
    """True when two service type URNs differ only by their version."""
    return a.rsplit(":", 1)[0] == b.rsplit(":", 1)[0]


def service_version(service_type: str) -> int:
    tail = service_type.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


class G(object):

    def __init__(self):
        self.http: aiohttp.ClientSession = None


g = G()


def unescape_xml(xml):
    return xml.decode().replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')


def xml2dict(xml):
    if not isinstance(xml, str):
        xml = unescape_xml(xml)
    parsed = xmltodict.parse(xml,
                             process_namespaces=True,
                             force_list=('service',),
                             namespaces={
                                 **UPNP_VERSIONED_NAMESPACES,
                                 "http://schemas.xmlsoap.org/soap/envelope/": None,
                                 "urn:schemas-upnp-org:event-1-0": None,
                                 "urn:schemas-upnp-org:metadata-1-0/AVT/": None
                             })
    return DotMap(parsed)


def pms_header(device):
    return {
        'X-Plex-Client-Identifier': device.uuid,
        'X-Plex-Device': device.model,
        'X-Plex-Device-Name': device.name,
        'X-Plex-Platform': settings.platform,
        'X-Plex-Platform-Version': settings.platform_version,
        'X-Plex-Product': device.model,
        'X-Plex-Version': settings.version,
        'X-Plex-Provides': 'player,pubsub-player'
    }


def plex_server_response_headers(device):
    return {
        'Accept': '*/*',
        'Connection': 'keep-alive',
        'Accept-Language': 'en',
        'X-Plex-Device': device.model,
        'X-Plex-Platform': settings.platform,
        'X-Plex-Platform-Version': settings.platform_version,
        'X-Plex-Product': device.model,
        'X-Plex-Version': settings.version,
        'X-Plex-Client-Identifier': device.uuid,
        'X-Plex-Device-Name': device.name,
        'X-Plex-Provides': 'player,pubsub-player',
    }


def subscriber_send_headers(device):
    return {
        'Content-Type': 'application/xml',
        'Connection': 'Keep-Alive',
        'X-Plex-Client-Identifier': device.uuid,
        'X-Plex-Platform': settings.platform,
        'X-Plex-Platform-Version': settings.platform_version,
        'X-Plex-Product': device.model,
        'X-Plex-Version': settings.version,
        'X-Plex-Device-Name': device.name,
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'en,*'
    }


def timeline_poll_headers(device):
    return {
        'X-Plex-Client-Identifier': device.uuid,
        'X-Plex-Protocol': '1.0',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Max-Age': '1209600',
        'Access-Control-Expose-Headers': 'X-Plex-Client-Identifier',
        'Content-Type': 'text/xml;charset=utf-8'
    }


def parse_timedelta(s):
    t = datetime.strptime(s, "%H:%M:%S")
    delta = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
    return delta


def convert_volume(value: int, from_max: int, from_min: int, to_max: int, to_min: int, to_step: int):
    if from_max == to_max and from_min == to_min:
        return value
    if from_max - from_min == to_max - to_min:
        return value - from_min + to_min
    percent = float(value - from_min) / float(from_max - from_min)
    value = percent * (to_max - to_min)
    value = int(value / to_step)
    value += to_min
    return value


def soap_response_body(info, action: str, device_name: str = ""):
    """Pull "<action>Response" out of a parsed SOAP envelope, complaining if absent.

    Returning None quietly here is how an unhandled namespace turns into "playback
    works but nothing ever updates", which is expensive to track down.
    """
    body = info.Envelope.Body
    key = f"{action}Response"
    try:
        keys = list(body.keys())
    except Exception:
        keys = []
    if key not in keys:
        print(f"dlna {device_name} {action}: no {key} in SOAP response, got {keys}")
        return None
    return body.get(key)
