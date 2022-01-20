import asyncio

from plex.adapters import adapter_by_device
from utils import subscriber_send_headers, pms_header, g
from settings import settings
from dlna import devices, get_device_by_uuid
from datetime import datetime, timedelta

TIMELINE_STOPPED = '<MediaContainer commandID="{command_id}">' \
                   '<Timeline type="music" state="stopped"/>' \
                   '<Timeline type="video" state="stopped"/>' \
                   '<Timeline type="photo" state="stopped"/>' \
                   '</MediaContainer>'


TIMELINE_DISCONNECTED = '<MediaContainer commandID="{command_id}" disconnected="1">' \
                        '<Timeline type="music" state="stopped"/>' \
                        '<Timeline type="video" state="stopped"/>' \
                        '<Timeline type="photo" state="stopped"/>' \
                        '</MediaContainer>'


CONTROLLABLE = 'playPause,stop,volume,shuffle,repeat,seekTo,skipPrevious,skipNext,stepBack,stepForward'

TIMELINE_PLAYING = '<MediaContainer commandID="{command_id}"><Timeline controllable="' + CONTROLLABLE + '" ' \
                   'type="music" {parameters}/><Timeline type="video" state="stopped"/><Timeline type="photo" ' \
                   'state="stopped"/></MediaContainer> '


class SubscribeManager(object):
    subscribers = {}
    running = True
    last_server_notify_state = {}

    def get_subscriber(self, target_uuid: str, client_uuid: str):
        s = [s for s in self.subscribers.get(target_uuid, []) if s.uuid == client_uuid]
        if len(s) > 0:
            return s[0]
        return None

    def update_command_id(self, target_uuid: str, client_uuid: str, command_id: int):
        s = self.get_subscriber(target_uuid, client_uuid)
        if s is not None:
            s.command_id = command_id

    def add_subscriber(self,
                       target_uuid: str,
                       client_uuid: str,
                       host: str,
                       port: int,
                       protocol: str = "http",
                       command_id: int = 0):
        print(f"add sub {client_uuid} to {target_uuid}")
        s = self.get_subscriber(target_uuid, client_uuid)
        if s is not None:
            if s.host != host or s.port != port or s.protocol != protocol:
                self.remove_subscriber(s.uuid)
            else:
                s.command_id = command_id
                return
        l = self.subscribers.get(target_uuid, [])
        l.append(Subscriber(client_uuid, host, port, self, protocol, command_id))
        self.subscribers[target_uuid] = l

    async def remove_subscriber(self, uuid, target_uuid: str = None):
        print(f"remove sub {uuid} from {target_uuid}")
        for tu in [target_uuid] if target_uuid is not None else self.subscribers.keys():
            l = self.subscribers.get(tu, [])
            remove = None
            for s in l:
                if s.uuid == uuid:
                    remove = s
                    break
            if remove in l:
                l.remove(remove)
            if len(l) == 0:
                device = await get_device_by_uuid(tu)
                if device is not None and len(self.subscribers.get(tu, [])) == 0:
                    device.stop_subscribe()

    def stop(self):
        self.running = False

    async def notify_server(self):
        await asyncio.gather(*[self.notify_server_device(device) for device in devices])

    async def notify_server_device(self, device, force=False):
        subs = self.subscribers.get(device.uuid, [])
        if len(subs) == 0 and not force:
            return
        adapter = adapter_by_device(device, device.port)
        if adapter.plex_lib is None or adapter.queue is None:
            return
        if adapter.no_notice and not force:
            print(f"ignore sub notice for server")
            return
        if adapter.plex_state is None:
            return
        if self.last_server_notify_state.get(device.uuid, "") == adapter.plex_state == "stopped" and not force:
            return
        self.last_server_notify_state[device.uuid] = adapter.plex_state
        params = await adapter.get_pms_state()
        if not params or params.get('state', None) is None:
            return
        params.update(pms_header(device))
        async with g.http.get(adapter.plex_lib.get_timeline(), params=params) as res:
            try:
                res.raise_for_status()
            except Exception as e:
                print(f"notify server error {e}, {res.content}, {params}")

    async def notify(self):
        await self.notify_server()
        tasks = [self.notify_device(device) for device in devices]
        await asyncio.gather(*tasks)

    async def msg_for_device(self, device):
        adapter = adapter_by_device(device, device.port)
        if adapter.no_notice:
            return None
        if adapter.state.state is None or adapter.state.state == "STOPPED" or adapter.queue is None:
            return TIMELINE_STOPPED
        state = await adapter.get_state()
        if not state or state.get('state', None) is None:
            return TIMELINE_STOPPED
        state['itemType'] = 'music'
        xml = TIMELINE_PLAYING.format(parameters=" ".join([f'{k}="{v}"' for k, v in state.items()]),
                                      command_id="{command_id}")
        return xml

    async def notify_device(self, device):
        subs = self.subscribers.get(device.uuid, [])
        adapter = adapter_by_device(device, device.port)
        if adapter.no_notice:
            print(f"ignore sub notice for {adapter.dlna.name}")
            return
        msg = await self.msg_for_device(device)
        if msg is None:
            return
        await asyncio.gather(*[sub.send(msg, device) for sub in subs])

    async def notify_device_disconnected(self, device):
        subs = self.subscribers.get(device.uuid, [])
        await asyncio.gather(*[sub.send(TIMELINE_DISCONNECTED, device) for sub in subs])
        await asyncio.gather(*[self.remove_subscriber(sub.uuid, target_uuid=device.uuid) for sub in subs])

    async def start(self):
        await self.notify()
        while self.running:
            await asyncio.sleep(settings.plex_notify_interval)
            wait_timeout = settings.plex_notify_interval * 10
            try:
                target_devices = []
                none_uuids = []
                for u, l in self.subscribers.items():
                    if len(l) > 0:
                        d = await get_device_by_uuid(u)
                        if d is not None:
                            target_devices.append(d)
                        else:
                            none_uuids.append(u)
                for u in none_uuids:
                    if u in self.subscribers:
                        del self.subscribers[u]
                if len(target_devices) == 0:
                    continue
                await asyncio.wait([asyncio.create_task(adapter_by_device(device, device.port).wait_for_event(wait_timeout))
                                    for device in target_devices],
                                   timeout=wait_timeout,
                                   return_when=asyncio.FIRST_EXCEPTION)
            except asyncio.exceptions.TimeoutError:
                pass
            try:
                await self.notify()
            except Exception as e:
                print(f"subscribe notify error {e}")


class Subscriber(object):

    def __init__(self, uuid, host, port, manager: SubscribeManager, protocol: str = "http", command_id: int = 0):
        self.uuid = uuid
        self.host = host
        self.port = port
        self.protocol = protocol
        self.command_id = command_id
        self.url = f"{protocol}://{host}:{port}/:/timeline"
        self.manager = manager

    async def send(self, msg: str, device):
        msg = msg.format(command_id=self.command_id)
        response = None
        # print(f"sub send {self.host} {msg}")
        try:
            async with g.http.post(self.url, data=msg, headers=subscriber_send_headers(device),
                                   timeout=1) as response:
                response.raise_for_status()
        except Exception as e:
            print(f"subscriber send error {self} {e} {await response.text() if response is not None else 'None'}")
            await self.manager.remove_subscriber(self.uuid)

    def __eq__(self, other):
        return self.uuid == other.uuid

    def __repr__(self):
        return f"{self.host}:{self.port}"


sub_man = SubscribeManager()

