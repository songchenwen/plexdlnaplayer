import asyncio
import socket

from settings import settings

SSDP_BROADCAST_PORT = 1900
SSDP_BROADCAST_ADDR = "239.255.255.250"

SSDP_BROADCAST_PARAMS = [
    "M-SEARCH * HTTP/1.1",
    "HOST: {0}:{1}".format(SSDP_BROADCAST_ADDR, SSDP_BROADCAST_PORT),
    "MAN: \"ssdp:discover\"", "MX: 10", "ST: ssdp:all", "", ""]
SSDP_BROADCAST_MSG = "\r\n".join(SSDP_BROADCAST_PARAMS)


SEND_INTERVAL_SECS = 30


def get_protocol(discover):

    class DlnaProtocol(object):

        def __init__(self):
            self.transport = None
            discover.protocol = self
            self.is_connected = False

        def connection_made(self, transport):
            self.transport = transport
            self.is_connected = True
            print("dlna discover connected")
            asyncio.create_task(self.send_loop())

        async def send_loop(self):
            while self.is_connected:
                self.transport.sendto(SSDP_BROADCAST_MSG.encode("UTF-8"),
                                      (SSDP_BROADCAST_ADDR, SSDP_BROADCAST_PORT))
                await asyncio.sleep(SEND_INTERVAL_SECS)

        def datagram_received(self, data, addr):
            info = [a.split(":", 1)
                    for a in data.decode("UTF-8").split("\r\n")[1:]]
            device = dict([(a[0].strip().lower(), a[1].strip())
                           for a in info if len(a) >= 2])
            asyncio.create_task(discover.on_new_device(device['location']))

        def error_received(self, exc):
            print('Error received:', exc)

        def connection_lost(self, exc):
            print("Socket closed, stop the event loop")
            self.is_connected = False
            self.transport = None

    return DlnaProtocol


class DlnaDiscover(object):

    def __init__(self, new_device_callback):
        self.new_device_callback = new_device_callback
        self.device_locations = []
        self.protocol = None
        self.socket = None

    async def on_new_device(self, location_url):
        if location_url not in self.device_locations:
            self.device_locations.append(location_url)
            await self.new_device_callback(location_url)

    def init_socket(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception as e:
            print(f"socket reuse failed {e}")

        self.socket.bind(("", SSDP_BROADCAST_PORT + 10))
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(SSDP_BROADCAST_ADDR) +
                               socket.inet_aton('0.0.0.0'))
        self.socket.setblocking(False)

    async def discover(self, loop=None):
        if settings.location_url is not None and len(settings.location_url) > 0:
            await self.on_new_device(settings.location_url)
            return
        self.init_socket()
        if loop is None:
            loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(get_protocol(self), sock=self.socket)
