from dlna.dlna_device import DlnaDevice
from utils import pms_header, xml2dict, g

PINS = 'https://plex.tv/api/v2/pins'
CHECKPINS = 'https://plex.tv/api/v2/pins/{pin_id}'


async def get_pin(device: DlnaDevice):
    async with g.http.post(PINS, headers=pms_header(device)) as p:
        p.raise_for_status()
        d = xml2dict(await p.text())
        return d.pin['@code'], d.pin['@id']


async def check_pin(pin_id, device: DlnaDevice):
    async with g.http.get(CHECKPINS.format(pin_id=pin_id), headers=pms_header(device)) as p:
        p.raise_for_status()
        d = xml2dict(await p.text())
        return d.pin['@authToken']
