import re
import traceback

import asyncio
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientConnectorError

from plex.adapters import remove_adapter
from utils import xml2dict, UPNP_RC_SERVICE_TYPE, UPNP_AVT_SERVICE_TYPE, g
from settings import settings

PAYLOAD_FMT = '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" ' \
              's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{urn}">' \
              '{fields}</u:{action}></s:Body></s:Envelope>'


DEFAULT_ACTION_DATA = {
    "InstanceID": 0,
    "Channel": "Master",
    "CurrentURIMetaData": "",
    "NextURIMetaData": "",
    "Unit": "REL_TIME",
    "Speed": 1
}

ERROR_COUNT_TO_REMOVE = 20

devices = []


class DlnaDeviceService(object):

    def __init__(self, service_dict: dict, device):
        self.service_type = service_dict['serviceType']
        self.control_url = urljoin(device.location_url, service_dict['controlURL'])
        self.event_url = urljoin(device.location_url, service_dict['eventSubURL'])
        self.spec_url = urljoin(device.location_url, service_dict['SCPDURL'])
        self.urn = self.service_type
        self.device = device
        self.subscribed = False
        self._spec_info = None
        self.next_subscribe_call_time = None

    def payload_from_template(self, action: str, data: dict):
        fields = ''
        for tag, value in data.items():
            fields += '<{tag}>{value}</{tag}>'.format(tag=tag, value=value)
        payload = PAYLOAD_FMT.format(action=action, urn=self.urn, fields=fields)
        return payload

    async def control(self, action: str, data: dict, client: aiohttp.ClientSession = None):
        headers = {
            'Content-type': 'text/xml',
            'SOAPACTION': '"{}#{}"'.format(self.urn, action),
            'charset': 'utf-8',
            'User-Agent': '{}/{}'.format(__file__, '1.0')
        }
        if client is None:
            client = g.http
        action_spec = await self.get_action_spec(action, client=client)
        if action_spec is None:
            raise Exception(f"No such action {action}, {self.service_type}")

        if action_spec.argumentList.argument:
            args = []
            if isinstance(action_spec.argumentList.argument, list):
                args = action_spec.argumentList.argument
            else:
                args = [action_spec.argumentList.argument]
            if not isinstance(data, dict):
                none_default_arguments = [argument for argument in args
                                          if argument.name not in DEFAULT_ACTION_DATA.keys()]
                if len(none_default_arguments) == 1:
                    data = {none_default_arguments[0].name: data}
                elif len(none_default_arguments) != 0:
                    raise Exception(f"{action} needs {len(none_default_arguments)} arguments, pass data as dict.")
            for argument in args:
                if argument.name in DEFAULT_ACTION_DATA.keys() and argument.name not in data.keys():
                    data[argument.name] = DEFAULT_ACTION_DATA[argument.name]
        payload = self.payload_from_template(action, data)

        try:
            async with client.post(self.control_url, data=payload.encode('utf8'), headers=headers, timeout=5) as response:
                if not response.ok:
                    raise Exception(f"service {self.control_url} {action} {response.status_code} {await response.text()}")
                self.device.repeat_error_count = 0
                info = xml2dict(await response.text())
                error = info.Envelope.Body.Fault.detail.UPnPError.get('errorDescription')
                if error is not None:
                    print(f"dlna device control request error {info.toDict()}")
                    return None
                return info.Envelope.Body.get(f"{action}Response")
        except Exception as e:
            print(f"dlna {self.device.name} {action} control error {e.__class__.__name__} {str(e)}")
            if "different loop" in str(e):
                traceback.print_tb(e.__traceback__)
            if isinstance(e, ClientConnectorError):
                self.device.repeat_error_count += 1
                if self.device.repeat_error_count >= ERROR_COUNT_TO_REMOVE:
                    print(f"remove device {self.device.name} due to {self.device.repeat_error_count} connection error")
                    if asyncio.get_running_loop() == self.device.loop:
                        asyncio.create_task(self.device.remove_self())
                    else:
                        asyncio.run_coroutine_threadsafe(self.device.remove_self(), self.device.loop)
            return None

    async def subscribe(self, timeout_sec=120):
        if settings.host_ip is None:
            print("dlna subscribe no host ip")
            return False
        if self.next_subscribe_call_time is not None:
            if datetime.utcnow() < self.next_subscribe_call_time:
                return
        headers = {
            'Cache-Control': 'no-cache',
            'User-Agent': '{}/{}'.format(__file__, '1.0'),
            'NT': 'upnp:event',
            'Callback': '<http://' + settings.host_ip + ':' + str(settings.http_port) + '/dlna/callback/'
                        + self.device.uuid + '>',
            'Timeout': f'Second-{timeout_sec}'
        }
        print(f"sub dlna device {self.device.name} {self.service_type}")
        async with g.http.request("SUBSCRIBE", self.event_url, headers=headers) as response:
            if response.ok:
                self.next_subscribe_call_time = datetime.utcnow() + timedelta(seconds=(timeout_sec // 2))
                return True
        return False

    async def get_spec(self, client: aiohttp.ClientSession = None):
        if self._spec_info is not None:
            return self._spec_info
        if client is None:
            client = g.http
        async with client.get(self.spec_url) as response:
            response.raise_for_status()
            xml = re.sub(" xmlns=\"[^\"]+\"", "", await response.text(), count=1)
            info = xml2dict(xml)
            self._spec_info = info
        return self._spec_info

    async def get_actions(self, client: aiohttp.ClientSession = None):
        spec = await self.get_spec(client=client)
        return spec['scpd']['actionList']['action']

    async def get_action_spec(self, action_name, client: aiohttp.ClientSession = None):
        for action in await self.get_actions(client=client):
            if action['name'] == action_name:
                return action
        return None

    async def get_state_variables(self):
        spec = await self.get_spec()
        return spec['scpd']['serviceStateTable']['stateVariable']


class DlnaDevice(object):

    def __init__(self, location_url):
        self.location_url = location_url
        self.name = None
        self.model = None
        self.ip = None
        self.info = None
        self.services = {}
        self.volume_max = None
        self.volume_min = None
        self.volume_step = None
        self.uuid = None
        self.loop = asyncio.get_running_loop()
        self.repeat_error_count = 0

    async def get_data(self):
        if self.info is None:
            async with g.http.get(self.location_url) as response:
                if response.ok:
                    xml = await response.text()
                    xml = re.sub(" xmlns=\"[^\"]+\"", "", xml, count=1)
                    info = xml2dict(xml)
                    info = info['root']
                    self.info = info
            if self.info:
                self.name = self.info['device']['friendlyName']
                self.model = settings.product
                if "modelDescription" in self.info['device'] and self.info['device']['modelDescription'] != None and self.info['device']['modelDescription'].strip() != "":
                    self.model = self.info['device']['modelDescription']
                self.uuid = self.info['device']['UDN'][len("uuid:"):]
                for service in self.info['device']['serviceList']['service']:
                    self.services[service['serviceType']] = DlnaDeviceService(service, self)
            if not self.name or not self.uuid:
                raise Exception(f"not valid dlna device {self.location_url}")
            if UPNP_AVT_SERVICE_TYPE not in self.services or UPNP_RC_SERVICE_TYPE not in self.services:
                raise Exception(f"not valid dlna device {self.name}")
            url = urlparse(self.location_url)
            self.ip = url.hostname
            self.name = settings.dlna_name_alias(self.uuid, self.name, self.ip)
            await self.get_volume_info()
            await asyncio.gather(*[s.get_spec() for s in self.services.values()])

    async def _find_service_by_action(self, action):
        await self.get_data()
        for t, service in self.services.items():
            a = await service.get_action_spec(action)
            if a is not None:
                return service
        return None

    def __getattr__(self, item):
        def action(data: dict = {}, client: aiohttp.ClientSession = None):
            return self.action(item, data=data, client=client)
        return action

    async def action(self, action: str, data: dict = {}, service_type: str = None, client: aiohttp.ClientSession = None):
        await self.get_data()
        service = None
        if service_type is not None:
            service = self._get_service(service_type)
            if service is None:
                raise Exception(f"service type not found {service_type}")
        else:
            service = await self._find_service_by_action(action)
            if service is None:
                raise Exception(f"action not found {action}")
        return await service.control(action, data, client=client)

    def _get_service(self, service_type: str):
        return self.services.get(service_type)

    async def subscribe(self, service_type: str = UPNP_AVT_SERVICE_TYPE, timeout_sec=120):
        await self.get_data()
        service = self._get_service(service_type)
        await service.subscribe(timeout_sec=timeout_sec)

    async def loop_subscribe(self, service_type: str = UPNP_AVT_SERVICE_TYPE, timeout_sec=120):
        service = self._get_service(service_type)
        if service.subscribed:
            return
        service.subscribed = True
        while service.subscribed:
            await self.subscribe(service_type=service_type, timeout_sec=timeout_sec)
            await asyncio.sleep(timeout_sec // 2)

    def stop_subscribe(self, service_type: str = UPNP_AVT_SERVICE_TYPE):
        service = self._get_service(service_type)
        service.subscribed = False

    async def get_volume_info(self):
        await self.get_data()
        self.volume_min = 0
        self.volume_max = 100
        self.volume_step = 1
        service = self._get_service(UPNP_RC_SERVICE_TYPE)
        try:
            vars = await service.get_state_variables()
            for v in vars:
                if v['name'] == "Volume":
                    r = v['allowedValueRange']
                    self.volume_min = int(r['minimum'])
                    self.volume_max = int(r['maximum'])
                    self.volume_step = int(r['step'])
                    break
        except Exception:
            pass

    async def remove_self(self):
        devices.remove(self)
        from plex.adapters import adapter_by_device, remove_adapter
        from plex.subscribe import sub_man
        self.stop_subscribe()
        adapter = adapter_by_device(self)
        adapter.state.state = "STOPPED"
        adapter.state.looping_wait_event.set()
        adapter.state._thread_should_stop = True
        await sub_man.notify_device_disconnected(self)
        await sub_man.notify_server_device(self, force=True)
        adapter.queue = None
        remove_adapter(adapter)

    def __str__(self):
        return self.name

    def __repr__(self):
        return " ".join(["DLNA Device", self.name, self.ip])

    def __eq__(self, other):
        return self.uuid == other.uuid


# if settings.location_url is not None:
#     devices.append(DlnaDevice(settings.location_url))


async def get_device_data():
    await asyncio.gather(*[device.get_data() for device in devices])


async def get_device_by_uuid(uuid):
    for device in devices:
        if device.uuid == uuid:
            await device.get_data()
            return device
    print(f"device uuid not found {uuid}")
    return None
