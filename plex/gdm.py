import socket
from settings import settings
import asyncio
import time

GDM_MULTICAST_ADDR = "239.0.0.250"
GDM_MULTICAST_PORT = 32413
GDM_PORT = 32412


def get_protocol(gdm):

    class ClientProtocol(object):

        def __init__(self):
            self.transport = None
            gdm.protocol = self
            self.is_connected = False

        def connection_made(self, transport):
            self.transport = transport
            self.is_connected = True
            # print(f"gdm connected {gdm.device.name}")
            self.transport.sendto(f"HELLO * HTTP/1.0\n{gdm.client_data}".encode('utf8'),
                                  (GDM_MULTICAST_ADDR, GDM_MULTICAST_PORT))

        def datagram_received(self, data, addr):
            if data.decode("utf8").startswith("M-SEARCH * HTTP/1."):
                if addr[0] == "127.0.0.1":
                    return
                try:
                    msg = f"HTTP/1.0 200 OK\n{gdm.client_data}"
                    # print(f"Reply {addr}, {gdm.device.name}")
                    self.transport.sendto(msg.encode('utf8'), addr)
                except Exception as e:
                    print(f"unable to send client message {e}")

        def error_received(self, exc):
            print('Error received:', exc)

        def connection_lost(self, exc):
            print("Socket closed, stop the event loop")
            self.is_connected = False
            self.transport = None

    return ClientProtocol


class PlexGDM(object):

    def __init__(self, device):
        self.server_port = settings.http_port
        self.socket = None
        self.protocol = None
        self.device = device

    # def notify_new_device(self, device):
    #     if self.protocol is not None and self.protocol.is_connected:
    #         for client_data in self.client_data(device=device):
    #             print(f"send gdm device {device.name}")
    #             self.protocol.transport.sendto(f"HELLO * HTTP/1.0\n{client_data}".encode('utf8'),
    #                                           (GDM_MULTICAST_ADDR, GDM_MULTICAST_PORT))

    @property
    def client_data(self):
        data = {
            "Name": self.device.name,
            "Port": str(self.server_port),
            "Content-Type": "plex/media-player",
            "Product": self.device.model,
            "Protocol": "plex",
            "Protocol-Version": "1",
            "Protocol-Capabilities": "timeline,playback,playqueues",
            "Version": settings.platform_version,
            "Resource-Identifier": self.device.uuid,
            "Updated-At": int(time.time()),
            "Device-Class": "stb"
        }
        client_data = ""
        for key, value in data.items():
            client_data += "%s: %s\n" % (key, value)
        return client_data

    def init_socket(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception as e:
            print(f"socket reuse failed {e}")

        self.socket.bind(("", GDM_PORT))
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GDM_MULTICAST_ADDR) +
                               socket.inet_aton('0.0.0.0'))
        self.socket.setblocking(False)

    def run(self, loop=None):
        self.init_socket()
        if loop is None:
            loop = asyncio.get_running_loop()
        asyncio.create_task(loop.create_datagram_endpoint(get_protocol(self), sock=self.socket))


if __name__ == "__main__":
    gdm = PlexGDM()
    loop = asyncio.get_event_loop()
    gdm.run(loop)
    loop.run_forever()
