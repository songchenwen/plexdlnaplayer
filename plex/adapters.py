import asyncio
from datetime import timedelta, datetime
import random
from threading import Thread, current_thread

import aiohttp
from dotmap import DotMap
from starlette.datastructures import QueryParams

from plex.play_queue import PlayQueue
from utils import parse_timedelta, convert_volume, g, pms_header
from settings import settings

adapters = {}


def adapter_by_device(device, query_params: QueryParams = None):
    a = adapters.get(device.uuid, None)
    if a is None:
        a = PlexDlnaAdapter(device, query_params)
        adapters[device.uuid] = a
    elif query_params is not None:
        a.plex_lib.update(query_params)
    return a


class PlexLib(object):

    def __init__(self):
        self.protocol = ''
        self.address = ''
        self.port = ''
        self.token = ''
        self.machine_id = ''

    def build_url(self, resource, token=True):
        url = f"{self.protocol}://{self.address}:{self.port}{resource}"
        if token:
            if "?" in resource:
                url += f"&X-Plex-Token={self.token}"
            else:
                url += f"?X-Plex-Token={self.token}"
        return url

    def update(self, query: QueryParams):
        if query is None:
            return
        self.protocol = query.get("protocol", self.protocol)
        self.address = query.get("address", self.address)
        self.port = int(query.get("port", self.port))
        self.token = query.get("token", self.token)
        self.machine_id = query.get("machineIdentifier", self.machine_id)

    def get_info(self):
        return dict(protocol=self.protocol,
                    address=self.address,
                    port=self.port,
                    machineIdentifier=self.machine_id)

    def get_queue(self, container_key):
        return PlayQueue(container_key, self)

    def get_timeline(self):
        return self.build_url("/:/timeline", token=False)


class DlnaState(object):
    changing_attrs = ("state", "volume", "elapsed", "current_uri", "current_track_duration", "muted")

    def __init__(self, adapter, state_change_callback=None):
        self.adapter = adapter
        self.dlna = adapter.dlna
        self._state = None
        self._volume = None
        self._elapsed = 0
        self._current_uri = None
        self._current_track_duration = None
        self._muted = None

        self.looping_thread: Thread = None
        self._thread_should_stop = False
        self.running_loop: asyncio.AbstractEventLoop = None
        self.state_change_callback = state_change_callback
        self._changed_state = None
        self.change_session_lock = None
        self._check_all_next_loop = False
        self.looping_wait_event: asyncio.Event = None
        self.last_access_time = datetime.utcnow()
        self.start_looping()

    def start_looping(self):
        print(f"{self.dlna} state start looping")
        self.looping_thread = Thread(target=self.background_loop,
                                     name=f"Dlna State Thread {str(self.dlna)}")
        self.looping_thread.start()

    def background_loop(self):
        self.running_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.running_loop)
        self.running_loop.run_until_complete(self._check_loop())

    def begin_change_session(self):
        self._changed_state = DotMap()

    def end_change_session(self):
        s = self._changed_state
        self._changed_state = None
        return s

    @property
    def check_all_next_loop(self):
        return self._check_all_next_loop

    @check_all_next_loop.setter
    def check_all_next_loop(self, value: bool):
        def set_value():
            if self._check_all_next_loop != value:
                self._check_all_next_loop = value
                if value:
                    self.looping_wait_event.set()
        if current_thread() == self.looping_thread:
            set_value()
        else:
            self.running_loop.call_soon_threadsafe(set_value)

    def __setattr__(self, key, value):
        if key in DlnaState.changing_attrs:
            old_value = self.__getattr__("_" + key)
            if old_value != value and self._changed_state is not None:
                self._changed_state[key] = value
                self._changed_state.old[key] = old_value
            object.__setattr__(self, "_" + key, value)
        else:
            object.__setattr__(self, key, value)

    def __getattr__(self, item):
        if item in DlnaState.changing_attrs:
            self.last_access_time = datetime.utcnow()
            if asyncio.get_running_loop() != self.running_loop:
                self.running_loop.call_soon_threadsafe(self.looping_wait_event.set)
            return object.__getattribute__(self, "_" + item)
        return object.__getattribute__(self, item)

    def __del__(self):
        self._thread_should_stop = True
        self.looping_thread.join()
        self.looping_thread = None

    def __repr__(self):
        return f"{self.dlna.name}: state {self.state} {self.elapsed} {self.volume} " \
               f"{self.muted} {self.current_track_duration} {self.current_uri}"

    async def check(self, client: aiohttp.ClientSession, check_count=0):
        position_check_count = 1
        volume_check_count = 12
        state_check_count = 10
        muted_check_count = 51
        checks = []
        results = []
        position_info = DotMap()
        state = DotMap()
        volume = DotMap()
        muted = DotMap()
        if check_count % position_check_count == 0 or self.check_all_next_loop:
            checks.append(self.dlna.GetPositionInfo(client=client))
            results.append(position_info)
        if check_count % state_check_count == 0 or self.state == "TRANSITIONING" or self.check_all_next_loop:
            checks.append(self.dlna.GetTransportInfo(client=client))
            results.append(state)
        if check_count % volume_check_count == 0 or self.check_all_next_loop:
            checks.append(self.dlna.GetVolume(client=client))
            results.append(volume)
        if check_count % muted_check_count == 0:
            checks.append(self.dlna.GetMute(client=client))
            results.append(muted)
        if self.check_all_next_loop:
            self.check_all_next_loop = False
        try:
            for idx, r in enumerate(await asyncio.gather(*checks)):
                results[idx].result = r
        except Exception as e:
            if __debug__:
                print(f"dlna {self.dlna.name} state loop error {str(e)}")
        self.begin_change_session()
        if position_info and position_info.result:
            position_info = position_info.result
            self.elapsed = int(parse_timedelta(position_info.RelTime).total_seconds() * 1000)
            self.current_uri = position_info.TrackURI
            self.current_track_duration = int(
                parse_timedelta(position_info.TrackDuration).total_seconds() * 1000)
            if not state and not self._changed_state and self.state in ("TRANSITIONING", "PLAYING"):
                if __debug__:
                    print(f"dlna {self.dlna.name} no eplased change? retry state")
                try:
                    state.result = await self.dlna.GetTransportInfo(client=client)
                except Exception:
                    pass
        if state and state.result:
            state = state.result
            self.state = state.CurrentTransportState
        if volume and volume.result:
            volume = volume.result
            volume = int(volume.CurrentVolume)
            self.volume = convert_volume(volume, self.dlna.volume_max, self.dlna.volume_min, 100, 0, 1)
        if muted and muted.result:
            muted = muted.result
            self.muted = muted.CurrentMute
        changed_state = self.end_change_session()
        if changed_state and self.state_change_callback:
            # if __debug__:
            #     print(f"{self.dlna.name} check loop {changed_state.toDict()} {self} in thread {current_thread().name}")
            self.state_change_callback(changed_state)

    @property
    def loop_interval(self):
        if datetime.utcnow() - self.last_access_time >= timedelta(seconds=90) \
                and self.state not in ("PLAYING", "TRANSITIONING"):
            return 60
        return 0.8

    async def wait_for_next_loop(self):
        try:
            await asyncio.wait_for(self.looping_wait_event.wait(), timeout=self.loop_interval)
        except Exception:
            pass
        self.looping_wait_event.clear()

    async def _check_loop(self):
        print(f"state loop {self.dlna.name} begin in {current_thread().name}")
        if self.change_session_lock is None:
            self.change_session_lock = asyncio.Lock()
        if self.looping_wait_event is None:
            self.looping_wait_event = asyncio.Event()
        async with aiohttp.ClientSession(loop=self.running_loop) as client:
            check_count = 0
            one_batch_count = 500
            while not self._thread_should_stop:
                async with self.change_session_lock:
                    await self.check(client, check_count=check_count)
                check_count += 1
                if check_count > one_batch_count:
                    check_count = 0
                await self.wait_for_next_loop()
        print(f"{self.dlna.name} state loop {self.dlna.name} stopped")
        self.running_loop = None

    def update(self, state: str = "", uri: str = "", position: str = ""):
        elapsed = ""
        if position:
            elapsed = int(parse_timedelta(position).total_seconds() * 1000)
        if (state == "" or self.state == state) and (uri == "" or self.current_uri == uri) and (elapsed == "" or self.elapsed == elapsed):
            return
        if self.running_loop is None:
            print(f"{self.dlna.name} state update discard due to no running loop {state} {uri} {position}")
            return
        if current_thread() == self.looping_thread:
            self.running_loop.create_task(self.update_in_thread(state=state, uri=uri, elapsed=elapsed))
        else:
            asyncio.run_coroutine_threadsafe(self.update_in_thread(state=state, uri=uri, elapsed=elapsed),
                                             self.running_loop)

    async def update_in_thread(self, state="", uri="", elapsed=""):
        if state == "":
            state = self.state
        if uri == "":
            uri = self.current_uri
        if elapsed == "":
            elapsed = self.elapsed
        if self.state == state and self.current_uri == uri and self.elapsed == elapsed:
            return
        if __debug__:
            print(f"{self.dlna.name} real update state from sub {state} {uri} {elapsed}")
        async with self.change_session_lock:
            if __debug__:
                print(f"{self.dlna.name} real update state from sub in lock {state} {uri} {elapsed}")
            self.begin_change_session()
            self.state = state
            self.current_uri = uri
            self.elapsed = elapsed
            changed = self.end_change_session()
            if changed and self.state_change_callback:
                self.state_change_callback(changed)


class PlexDlnaAdapter(object):

    def __init__(self, dlna, query: QueryParams = None):
        print(f"init adapter for {dlna} in thread {current_thread().name}")
        self.dlna = dlna
        self.plex_lib = PlexLib()
        if query is not None:
            self.plex_lib.update(query)
        self.queue = None
        self.state: DlnaState = DlnaState(self, self.state_changed_callback)
        self.shuffle = 0
        self.plex_bind_token = settings.get_token_for_uuid(self.dlna.uuid)
        self.no_notice = False
        self.loop = asyncio.get_running_loop()
        self.wait_state_change_events = []
        self.delay_stop_state_looping_task: asyncio.Task = None
        self.waiting_sub = 0
        self.current_track_info = None

    def check_auto_next(self, changed: DotMap):
        if self.queue is None:
            return False
        if changed.state and changed.state != "PLAYING" and changed.old.state == "TRANSITIONING":
            return False

        async def auto_next():
            if self.queue.repeat == 1:
                await self.play_selected_queue_item()
            elif self.queue.repeat == 2 and \
                    (await self.queue.selected_offset()) >= (await self.queue.total_count() - 1) and \
                    self.shuffle == 0:
                await self.queue.set_selected_offset(0)
                await self.play_selected_queue_item()
            else:
                await self.next()

        if self.state.current_uri is not None and not changed.state and not changed.uri and self.current_track_info:
            if (changed.elapsed == 0 < changed.old.elapsed <= self.current_track_info.duration
                and self.current_track_info.duration - changed.old.elapsed <= 2000) \
                    or (
                    changed.elapsed and changed.elapsed > changed.old.elapsed and
                    self.current_track_info.duration // 1000 * 1000 <= changed.elapsed <= self.current_track_info.duration):
                self.no_notice = True
                print(f"auto next stopped {self.state.state}, elapsed: {changed.old.elapsed} -> {changed.elapsed}, "
                      f"{self.current_track_info.duration}")
                self.state.update(state="TRANSITIONING", uri=None)
                asyncio.run_coroutine_threadsafe(auto_next(), self.loop)
                self.no_notice = False
                return True
        elif not changed.uri and changed.old.state == "PLAYING" and changed.state == "STOPPED" and self.state.current_track_duration - self.state.elapsed <= 1:
            self.no_notice = True
            print(f"auto next transitioning {changed.old.state} {changed.state}")
            self.state.update(state="TRANSITIONING", uri=None)
            asyncio.run_coroutine_threadsafe(auto_next(), self.loop)
            self.no_notice = False
            return True
        return False

    def state_changed_callback(self, changed_state: DotMap):
        if self.loop.is_closed():
            return
        if __debug__ or 'elapsed' not in changed_state.keys() or len(changed_state.keys()) > 2 or \
                not (0 <= changed_state.elapsed - changed_state.old.elapsed <= 1000):
            print(f"{self.dlna.name} state change notified {changed_state.toDict()}")
        n = self.check_auto_next(changed_state)
        if not n:
            asyncio.run_coroutine_threadsafe(self.state_changed(changed_state), self.loop)

    async def state_changed(self, changed_state: DotMap):
        removed_event = []
        for e in self.wait_state_change_events:
            if not e['interesting_fields']:
                e['event'].set()
                removed_event.append(e)
                continue
            for f in e['interesting_fields']:
                if f in changed_state.keys():
                    e['event'].set()
                    removed_event.append(e)
                    continue
            if "elapsed_jump" in e['interesting_fields']:
                if "elapsed" in changed_state and not (0 <= changed_state.elapsed - changed_state.old.elapsed <= 1000):
                    e['event'].set()
                    removed_event.append(e)
                    continue
        for r in removed_event:
            self.wait_state_change_events.remove(r)

    async def wait_for_event(self, timeout=None, interesting_fields=None):
        event = asyncio.Event()
        self.wait_state_change_events.append(dict(event=event, interesting_fields=interesting_fields))
        if len(self.wait_state_change_events) > 3:
            e = self.wait_state_change_events.pop()
            e['event'].set()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.exceptions.TimeoutError:
            pass

    async def play_media(self, container_key, key=None, offset=0, paused=False, query_params: QueryParams = None):
        if query_params is not None:
            self.plex_lib.update(query_params)
        self.state.update(uri=None)
        self.queue = self.plex_lib.get_queue(container_key)
        await self.queue.get_info()
        await self.play_selected_queue_item(offset=offset, paused=paused)

    async def play_selected_queue_item(self, offset=0, paused=False):
        self.state.update(state="TRANSITIONING")
        self.state.check_all_next_loop = True
        track = await self.queue.selected_track()
        url = self.queue.url_for_track(track)
        if url == self.state.current_uri:
            self.state.update(uri=None)
        await self.dlna.SetAVTransportURI(url)
        self.current_track_info = track
        if offset != 0:
            await self.dlna.Seek(str(timedelta(milliseconds=offset)))
        if paused:
            await self.pause()
        else:
            await asyncio.sleep(1)
            if self.state != "PLAYING":
                await self.play()

    async def refresh_queue(self, playQueueID):
        await self.queue.refresh_queue(playQueueID)
        while len(self.wait_state_change_events) > 0:
            e = self.wait_state_change_events.pop()
            e['event'].set()

    async def play(self):
        await self.dlna.Play()
        self.state.check_all_next_loop = True

    async def stop(self):
        self.state.update(state="STOPPED", uri=None)
        self.current_track_info = None
        await self.dlna.Stop()
        self.state.check_all_next_loop = True

    async def pause(self):
        self.state.update(state="PAUSED_PLAYBACK")
        await self.dlna.Pause()
        self.state.check_all_next_loop = True

    async def prev(self):
        if self.state.elapsed <= 5 * 1000:
            await self.next(revert=True)
        else:
            await self.seek(0)

    async def next(self, revert=False):
        direction = -1 if revert else 1
        current_offset = await self.queue.selected_offset()
        if self.shuffle > 0 and await self.queue.allow_shuffle():
            current_offset = random.choice(range(await self.queue.total_count()))
        else:
            current_offset += direction
        if current_offset >= await self.queue.total_count() or current_offset < 0:
            await self.stop()
            return
        self.state.update(state="TRANSITIONING")
        print(f"will play position {current_offset}")
        await self.queue.set_selected_offset(current_offset)
        await self.play_selected_queue_item()

    async def skip_to_track(self, key):
        self.state.update(state="TRANSITIONING")
        await self.queue.select_track_key(key)
        await self.play_selected_queue_item()

    async def seek(self, offset):
        await self.dlna.Seek(str(timedelta(milliseconds=offset)))

    async def get_elapsed(self):
        position_info = await self.dlna.GetPositionInfo()
        if position_info is None:
            return 0
        t = position_info.RelTime
        t = parse_timedelta(t)
        return int(t.total_seconds() * 1000)

    async def get_volume(self):
        volume = await self.dlna.GetVolume()
        volume = int(volume.CurrentVolume)
        return convert_volume(volume, self.dlna.volume_max, self.dlna.volume_min, 100, 0, 1)

    async def set_volume(self, volume):
        volume = convert_volume(volume, 100, 0, self.dlna.volume_max, self.dlna.volume_min, self.dlna.volume_step)
        await self.dlna.SetVolume(volume)
        self.state.check_all_next_loop = True

    async def is_muted(self):
        mute = await self.dlna.GetMute()
        return mute.CurrentMute

    def start_plex_tv_notify(self):
        asyncio.create_task(self._update_plex_tv_connection_loop())

    async def _update_plex_tv_connection_loop(self):
        while True:
            try:
                await self.update_plex_tv_connection()
            except Exception:
                pass
            await asyncio.sleep(60)

    async def update_plex_tv_connection(self):
        if not settings.host_ip:
            return
        if not self.plex_bind_token:
            self.plex_bind_token = settings.get_token_for_uuid(self.dlna.uuid)
            if not self.plex_bind_token:
                return
        await g.http.put(f"https://plex.tv/devices/{self.dlna.uuid}?X-Plex-Token={self.plex_bind_token}",
                         data={"Connection[][uri]": f"http://{settings.host_ip}:{settings.http_port}"},
                         headers=pms_header(self.dlna))

    def update_state(self, info):
        if info.propertyset:
            info = info.propertyset.property.LastChange.Event.InstanceID
        else:
            return
        state = info.TransportState['@val']
        uri = info.AVTransportURI['@val']
        pos = info.RelativeTimePosition['@val']
        if not state and not uri and not pos:
            print("ignoring notice no info")
            return
        if not state:
            state = ""
        if not uri:
            uri = ""
        if not pos:
            pos = ""
        if __debug__:
            print(f"{self.dlna.name} update state from sub {state} {uri} {pos}")
        self.state.update(state=state, uri=uri, position=pos)

    @property
    def plex_state(self):
        if self.state.state is None:
            return None
        if self.state.state == "PLAYING":
            return "playing"
        if self.state.state == "STOPPED":
            return "stopped"
        if self.state.state == "NO_MEDIA_PRESENT":
            return "stopped"
        if self.state.state == "PAUSED_PLAYBACK":
            return "paused"
        if self.state.state == "TRANSITIONING":
            return "playing"

    async def get_pms_state(self):
        if self.state is None:
            return None
        d = await self.get_state()
        keys = ['state', 'ratingKey', 'key', 'time', 'duration', 'playQueueItemID', 'shuffle', 'repeat', 'containerKey']
        not_wanted_keys = []
        for k, _ in d.items():
            if k not in keys:
                not_wanted_keys.append(k)
        for k in not_wanted_keys:
            del d[k]
        d['X-Plex-Token'] = self.plex_lib.token
        return d

    async def get_state(self):
        if self.state == "STOPPED" or self.state is None or self.queue is None:
            return {}
        lib_info = self.plex_lib.get_info()
        shuffle = self.shuffle
        if shuffle > 0 and not await self.queue.allow_shuffle():
            shuffle = 0
        track_info = await self.queue.get_track_info()
        time = self.state.elapsed
        volume = self.state.volume
        mute = self.state.muted
        state = {
            'state': self.plex_state,
            'time': time,
            'volume': volume,
            'mute': mute,
            'shuffle': shuffle,
            'repeat': self.queue.repeat
        }
        state.update(track_info)
        state.update(lib_info)
        return state

    def __del__(self):
        self.state._thread_should_stop = True
        del self.state
