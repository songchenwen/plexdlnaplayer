import re

from dlna.dlna_device import DlnaDevice
from utils import pms_header, xml2dict, g

PINS = 'https://plex.tv/api/v2/pins'
CHECKPINS = 'https://plex.tv/api/v2/pins/{pin_id}'


class PinError(Exception):
    pass


async def _raise_for_status(response, device: DlnaDevice):
    # plex.tv explains a rejection in the body (e.g. "X-Plex-Client-Identifier is
    # missing"), which raise_for_status() throws away.
    if response.ok:
        return
    body = await response.text()
    reasons = re.findall(r'message="([^"]*)"', body)
    reason = "; ".join(reasons) or body.strip()[:200] or response.reason
    raise PinError(f"plex.tv {response.status} for {device.name} "
                   f"(uuid={device.uuid!r}, model={device.model!r}): {reason}")


async def get_pin(device: DlnaDevice):
    async with g.http.post(PINS, headers=pms_header(device)) as p:
        await _raise_for_status(p, device)
        d = xml2dict(await p.text())
        return d.pin['@code'], d.pin['@id']


async def check_pin(pin_id, device: DlnaDevice):
    async with g.http.get(CHECKPINS.format(pin_id=pin_id), headers=pms_header(device)) as p:
        await _raise_for_status(p, device)
        d = xml2dict(await p.text())
        return d.pin['@authToken']
