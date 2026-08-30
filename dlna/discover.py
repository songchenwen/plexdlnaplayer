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
                # sendto raises while the interface is down or changing, and an
                # escape here ends discovery for the life of the process: no
                # M-SEARCH is ever broadcast again and no renderer is found.
                try:
                    self.transport.sendto(SSDP_BROADCAST_MSG.encode("UTF-8"),
                                          (SSDP_BROADCAST_ADDR, SSDP_BROADCAST_PORT))
                except Exception as e:
                    print(f"dlna discover broadcast failed {e.__class__.__name__} {e}")
                await asyncio.sleep(SEND_INTERVAL_SECS)

        def datagram_received(self, data, addr):
            # The socket is joined to the multicast group, so this receives
            # NOTIFY traffic as well as answers to our M-SEARCH. ssdp:byebye
            # carries no LOCATION, and anything on the network may send a
            # payload that is not UTF-8, so neither can be assumed here.
            try:
                text = data.decode("UTF-8")
            except UnicodeDecodeError:
                return
            info = [a.split(":", 1) for a in text.split("\r\n")[1:]]
            device = dict([(a[0].strip().lower(), a[1].strip())
                           for a in info if len(a) >= 2])
            location = device.get('location')
            if not location:
                return
            asyncio.create_task(discover.on_new_device(location))

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
        """Hand a discovered URL to the callback unless it was turned away before.

        Only rejected URLs are remembered. They are the expensive ones: every
        sweep would otherwise re-fetch the description of every printer and light
        bridge on the network. A URL the callback accepted costs nothing to offer
        again, because the callback already returns immediately for a renderer it
        holds, and offering it again is what lets a renderer that was removed
        after ERROR_COUNT_TO_REMOVE failures come back on the next sweep instead
        of staying missing until the bridge restarts.
        """
        if location_url in self.device_locations:
            return
        # Claim the URL before awaiting. SSDP answers arrive in bursts and
        # datagram_received starts a task per packet, so leaving the mark until
        # after the callback lets every task in a burst past this check and
        # fetch the same description several times over.
        self.device_locations.append(location_url)
        if await self.new_device_callback(location_url):
            self.device_locations.remove(location_url)

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
