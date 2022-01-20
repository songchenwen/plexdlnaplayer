
from fastapi import FastAPI, Request, Header, Query, HTTPException, Form
from fastapi.responses import Response
import uvicorn

from dlna import get_device_by_uuid, get_device_by_port, get_device_data, DlnaDiscover, devices
from plex.subscribe import sub_man
from utils import plex_server_response_headers, xml2dict, timeline_poll_headers, g
from settings import settings
import asyncio
from dlna.dlna_device import DlnaDevice
from plex.adapters import adapter_by_device
from plex.gdm import PlexGDM
from fastapi.templating import Jinja2Templates
from plex import pin_login
from datetime import datetime, timedelta
import aiohttp
import signal
import socket


XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'
XML_OK = XML_HEADER + '<Response code="200" status="OK"/>'

templates = Jinja2Templates(directory="templates")

plex_server = FastAPI()
s = plex_server
startup_done = False
primary_server = None


async def run_uvicorn(app, **kwargs):
    config = uvicorn.config.Config(app, **kwargs)
    server = uvicorn.Server(config=config)
    asyncio.create_task(server.serve())
    return server


def run_primary_uvicorn(app, **kwargs):
    global primary_server
    config = uvicorn.config.Config(app, **kwargs)
    primary_server = uvicorn.Server(config=config)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(primary_server.serve())


async def on_new_dlna_device(location_url):
    print(f"got new dlna deviec location url {location_url}")
    for d in devices:
        if d.location_url == location_url:
            return
    device = DlnaDevice(location_url)
    try:
        await device.get_data()
    except Exception as ex:
        print(f'Got Exception {ex}')
        return
    print(f"got new dlna device from {device.name}")
    new_port = settings.allocate_new_port()
    device.server = await run_uvicorn("plex:plex_server", host="0.0.0.0", port=new_port, loop="none", lifespan="on")
    asyncio.create_task(device.loop_subscribe(port=new_port), name=f"dlna sub {device.name}")
    devices.append(device)
    adapter = adapter_by_device(device, port=new_port)
    adapter.start_plex_tv_notify()
    gdm = PlexGDM(device, new_port)
    gdm.run()


dlna_discover = DlnaDiscover(on_new_dlna_device)


def guess_host_ip(request: Request):
    if settings.host_ip is not None:
        return
    if request.url.hostname.startswith("127.0.0"):
        return
    settings.host_ip = request.url.hostname
    print(f"guessed host ip {settings.host_ip}")
    for device in devices:
        adapter = adapter_by_device(device, device.port)
        asyncio.create_task(adapter.update_plex_tv_connection())


async def build_response(content: str, device: DlnaDevice = None, target_uuid: str = None, status_code: int = 200,
                         headers=None):
    if device is None and target_uuid is None:
        raise Exception("device and target uuid cannot both be none")
    if device is None:
        device = await get_device_by_uuid(target_uuid)
    if device is None:
        if headers is None:
            headers = {
                'Accept': '*/*',
                'Connection': 'keep-alive',
                'Accept-Language': 'en'}
            if target_uuid is not None:
                headers['X-Plex-Client-Identifier'] = target_uuid
    if headers is None:
        headers = plex_server_response_headers(device)
    return Response(content=content,
                    status_code=status_code,
                    headers=headers)


@s.on_event("startup")
async def on_startup():
    global startup_done
    if not startup_done:
        g.http = aiohttp.ClientSession()
        await dlna_discover.discover()
        asyncio.create_task(sub_man.start())
        await get_device_data()
        startup_done = True


@s.on_event("shutdown")
async def on_shutdown():
    global primary_server
    sub_man.stop()
    stop_tasks = []
    for device in devices:
        adapter = adapter_by_device(device, port=0)
        stop_tasks.append(adapter.stop())
        stop_tasks.append(device.remove_self())
    await asyncio.gather(*stop_tasks)
    if g.http:
        await g.http.close()
    primary_server.should_exit = True
    primary_server.force_exit = True
    await primary_server.shutdown()


@s.get("/")
async def link_page(request: Request):
    guess_host_ip(request)
    ds = []
    for d in devices:
        adapter = adapter_by_device(d, d.port)
        if adapter.plex_bind_token is not None:
            ds.append(dict(
                name=d.name,
                uuid=d.uuid,
                binded=True
            ))
        else:
            pin, pin_id = await pin_login.get_pin(d)
            ds.append(dict(
                name=d.name,
                uuid=d.uuid,
                pin=pin,
                pin_id=pin_id,
                binded=False
            ))
    return templates.TemplateResponse("bind.html", {'devices': ds, 'request': request})


@s.post("/")
async def link_device(request: Request,
                      name: str = Form(default=None),
                      uuid: str = Form(...),
                      pin_id: str = Form(default=None)):
    device = await get_device_by_uuid(uuid)
    if device is None:
        raise HTTPException(404, f"device not found {uuid}")
    adapter = adapter_by_device(device, device.port)
    if pin_id:
        token = await pin_login.check_pin(pin_id, device)
        if token:
            settings.set_token_for_uuid(uuid, token)
            await adapter.update_plex_tv_connection()
    if name and name != device.name:
        device.name = name
        settings.save_dlna_name_alias(uuid, name)
        await adapter.update_plex_tv_connection()
    return await link_page(request)


@s.api_route("/dlna/callback/{uuid}", methods=["NOTIFY"])
async def dlna_subscribe(request: Request, uuid: str):
    adapter = adapter_by_device(await get_device_by_uuid(uuid), request.url.port)
    b = await request.body()
    info = xml2dict(b)
    if adapter is not None:
        adapter.update_state(info)
    return ""


@s.get("/player/playback/playMedia")
async def play_media(request: Request,
                     commandID: int,
                     containerKey: str,
                     key: str,
                     offset: int = 0,
                     paused: bool = False,
                     type_: str = Query("music", alias="type"),
                     target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                     client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    guess_host_ip(request)
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404)
    adapter = adapter_by_device(device, device.port, request.query_params)
    if type_ == "music":
        await adapter.play_media(containerKey, key=key, offset=offset, paused=paused, query_params=request.query_params)
    else:
        await adapter.stop()
    return await build_response("", device=device)


@s.get("/player/playback/refreshPlayQueue")
async def refresh_play_queue(commandID: int,
                             playQueueID: int,
                             target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                             client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404)
    adapter = adapter_by_device(device, device.port)
    await adapter.refresh_queue(playQueueID)
    return await build_response("", device=device)


@s.get("/player/playback/play")
async def play(commandID: int,
               type_: str = Query("music", alias="type"),
               target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
               client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404)
    adapter = adapter_by_device(device, device.port)
    if type_ == "music":
        await adapter.play()
    else:
        await adapter.stop()
    return await build_response("", device=device)


@s.get("/player/playback/pause")
async def pause(commandID: int,
                type_: str = Query("music", alias="type"),
                target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404)
    adapter = adapter_by_device(device, device.port)
    if type_ == "music":
        await adapter.pause()
    return await build_response("", device=device)


@s.get("/player/playback/stop")
async def stop(request: Request,
               commandID: int,
               type_: str = Query("music", alias="type"),
               target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
               client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    guess_host_ip(request)
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == "music":
        device = await get_device_by_uuid(target_uuid)
        adapter = adapter_by_device(device, request.url.port)
        await adapter.stop()
    return await build_response(XML_OK, target_uuid=target_uuid)


@s.get("/player/playback/skipNext")
async def next_(commandID: int,
                type_: str = Query("music", alias="type"),
                target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == "music":
        device = await get_device_by_uuid(target_uuid)
        if device is None:
            raise HTTPException(404, f"device not found {target_uuid}")
        adapter = adapter_by_device(device, device.url.port)
        await adapter.next()
    return await build_response("", target_uuid=target_uuid)


@s.get("/player/playback/skipPrevious")
async def prev(commandID: int,
               type_: str = Query("music", alias="type"),
               target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
               client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == "music":
        device = await get_device_by_uuid(target_uuid)
        if device is None:
            raise HTTPException(404, f"device not found {target_uuid}")
        adapter = adapter_by_device(device, device.port)
        await adapter.prev()
    return await build_response("", target_uuid=target_uuid)


@s.get("/player/playback/seekTo")
async def seek(commandID: int,
               offset: int,
               type_: str = Query("music", alias="type"),
               target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
               client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == "music":
        device = await get_device_by_uuid(target_uuid)
        if device is None:
            raise HTTPException(404, f"device not found {target_uuid}")
        adapter = adapter_by_device(device, device.port)
        await adapter.seek(offset)
    return await build_response("", target_uuid=target_uuid)


@s.get("/player/playback/skipTo")
async def skip_to(commandID: int,
                  key: str,
                  type_: str = Query("music", alias="type"),
                  target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                  client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == "music":
        device = await get_device_by_uuid(target_uuid)
        if device is None:
            raise HTTPException(404, f"device not found {target_uuid}")
        adapter = adapter_by_device(device, device.port)
        await adapter.skip_to_track(key)
    return await build_response("", target_uuid=target_uuid)


@s.get("/player/playback/setParameters")
async def set_parameters(commandID: int,
                         type_: str = Query("music", alias="type"),
                         shuffle: int = None,
                         repeat: int = None,
                         volume: float = None,
                         target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                         client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    if type_ == 'music':
        device = await get_device_by_uuid(target_uuid)
        if device is None:
            raise HTTPException(404, f"device not found {target_uuid}")
        adapter = adapter_by_device(device, device.port)
        if shuffle is not None:
            adapter.shuffle = shuffle
        if repeat is not None:
            adapter.queue.repeat = repeat
        if volume is not None:
            await adapter.set_volume(int(volume))
    return await build_response("", target_uuid=target_uuid)


waiting_poll_count = 0
@s.get("/player/timeline/poll")
async def timeline_poll(request: Request,
                        commandID: int,
                        wait: int = 0,
                        target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                        client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    global waiting_poll_count
    waiting_poll_count += 1
    if waiting_poll_count > 3:
        print(f"waiting poll {waiting_poll_count}")
    begin_time = datetime.utcnow()
    guess_host_ip(request)
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404, f"device not found {target_uuid}")
    asyncio.create_task(device.loop_subscribe(port=request.url.port))
    adapter = adapter_by_device(device, device.port)
    if wait == 1:
        await adapter.wait_for_event(settings.plex_notify_interval * 20, interesting_fields=[
            'state', 'volume', 'current_uri', 'elapsed_jump'])
    msg = await sub_man.msg_for_device(device)
    while msg is None:
        print(f"waiting for msg {target_uuid}")
        await asyncio.sleep(settings.plex_notify_interval)
        msg = await sub_man.msg_for_device(device)
    msg = msg.format(command_id=commandID)
    if datetime.utcnow() - begin_time >= timedelta(milliseconds=500):
        print(f"{request.url} used {datetime.utcnow() - begin_time}")
    waiting_poll_count -= 1
    asyncio.create_task(sub_man.notify_server_device(device, force=True))
    return await build_response(msg, device=device, headers=timeline_poll_headers(device))


@s.get("/player/timeline/subscribe")
async def subscribe(request: Request,
                    commandID: int,
                    port: int,
                    protocol: str = "http",
                    target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                    client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    guess_host_ip(request)
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404, f"device not found {target_uuid}")
    sub_man.add_subscriber(target_uuid, client_uuid, request.client.host, port, protocol=protocol, command_id=commandID)
    return await build_response(XML_OK, target_uuid=target_uuid)


@s.get("/player/timeline/unsubscribe")
async def unsubscribe(request: Request,
                      commandID: int,
                      target_uuid: str = Header(None, alias="x-plex-target-client-identifier"),
                      client_uuid: str = Header(None, alias="x-plex-client-identifier")):
    guess_host_ip(request)
    sub_man.update_command_id(target_uuid, client_uuid, commandID)
    await sub_man.remove_subscriber(client_uuid, target_uuid=target_uuid)
    return await build_response(XML_OK, target_uuid=target_uuid)


@s.get("/resources")
async def resources(request: Request, target_uuid: str = Header(None, alias="x-plex-target-client-identifier")):
    guess_host_ip(request)
    port = request.url.port
    device = await get_device_by_port(port)
    if device is None:
        raise HTTPException(404, f"no device matches port {port}")

    print(f"resource for {device.name}")
    res = "<MediaContainer>"
    res += f'<Player title="{device.name}" protocol="plex" protocolVersion="1" ' \
           f'protocolCapabilities="timeline,playback,playqueues" ' \
           f'machineIdentifier="{device.uuid}" product="{device.model}" ' \
           f'platform="{settings.platform}" ' \
           f'platformVersion="{settings.platform_version}" ' \
           f'version="{settings.version}" deviceClass="stb"/>'
    res += "</MediaContainer>"
    return await build_response(res, device=device)


@s.get("/player/mirror/details")
async def mirror(target_uuid: str = Header(None, alias="x-plex-target-client-identifier")):
    device = await get_device_by_uuid(target_uuid)
    if device is None:
        raise HTTPException(404, f'device not found {target_uuid}')
    return await build_response("", target_uuid=target_uuid)


def start_plex_server(port=None):
    if port is None:
        port = settings.http_port
    if settings.host_ip is None:
        try:
            host_name = socket.gethostname()
            settings.host_ip = socket.gethostbyname(host_name)
            print(f"Guessed host IP as {settings.host_ip}")
        except:
            print(f"Could not guess host IP, use HOST_IP in startup.")
            pass

    run_primary_uvicorn("plex:plex_server", host="0.0.0.0", port=port, loop="none")
