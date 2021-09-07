from dotmap import DotMap
from starlette.datastructures import URL, QueryParams
import math

from utils import g

UNLIMITED = math.inf


class PlayQueue(object):

    @classmethod
    def from_url(cls, url):
        from plex.adapters import PlexLib
        url = URL(url)
        plex_lib = PlexLib()
        plex_lib.protocol = url.scheme
        plex_lib.address = url.hostname
        plex_lib.port = url.port
        q = QueryParams(url.query)
        plex_lib.token = q.get("X-Plex-Token")
        return PlayQueue(url.path + "?" + url.remove_query_params("X-Plex-Token").query,
                         plex_lib)

    def __init__(self, container_key, plex_lib):
        self.container_key = container_key
        self.plex_lib = plex_lib
        self.info = None
        self.start_offset = None
        self.repeat = 0

    async def get_info(self):
        if self.info is None:
            url = self.plex_lib.build_url(self.container_key)
            print(f"get queue {url}")
            async with g.http.get(url, headers={"Accept": "application/json"}) as res:
                res.raise_for_status()
                self.info = DotMap((await res.json())['MediaContainer'])
                for idx, track in enumerate(await self.available_tracks()):
                    if track.playQueueItemID == await self.selected_item_id():
                        self.start_offset = await self.selected_offset() - idx
                        break
        return self.info

    async def refresh_queue(self, playQueueID):
        if playQueueID != self.info.playQueueID:
            print(f"refresh to a different queue? {self.info.playQueueID} -> {playQueueID}")
            self.container_key = str(self.container_key).replace(str(self.info.playQueueID), str(playQueueID), 1)
        old_selected_item_id = await self.selected_item_id()
        old_selected_item_offset = await self.selected_offset()
        url = self.plex_lib.build_url(self.container_key)
        print(f"refresh queue from {url}")
        async with g.http.get(url, headers={"Accept": "application/json"}) as res:
            res.raise_for_status()
            info = DotMap((await res.json())['MediaContainer'])
            found = 0
            new_available_offset = None
            start_offset = None
            for idx, track in enumerate(info.Metadata):
                if track.playQueueItemID == old_selected_item_id:
                    new_available_offset = idx
                    found += 1
                if track.playQueueItemID == info.playQueueSelectedItemID:
                    start_offset = info.playQueueSelectedItemOffset - idx
                    found += 1
                if found >= 2:
                    break
            if new_available_offset is None or start_offset is None:
                raise Exception("refreshed queue has no current selected item?")
            selected_offset = new_available_offset + start_offset
            print(f"refreshed queue info SelectedItemOffset {old_selected_item_offset} -> {selected_offset}, "
                  f"start_offset {self.start_offset} -> {start_offset}")
            info.playQueueSelectedItemID = old_selected_item_id
            info.playQueueSelectedItemOffset = selected_offset
            self.info = info
            self.start_offset = start_offset

    async def set_selected_offset(self, offset):
        assert 0 <= offset < await self.total_count()
        info = await self.get_info()
        if offset > self.last_offset:
            await self.more(after=True)
            await self.set_selected_offset(offset)
        elif offset < self.start_offset:
            await self.more(after=False)
            await self.set_selected_offset(offset)
        else:
            info.playQueueSelectedItemOffset = offset
            info.playQueueSelectedItemID = (await self.selected_track()).playQueueItemID

    async def track(self, offset):
        if self.info is None:
            await self.get_info()
        assert 0 <= offset < await self.total_count()
        if offset > self.last_offset:
            await self.more(after=True)
            return await self.track(offset)
        elif offset < self.start_offset:
            await self.more(after=False)
            return await self.track(offset)
        else:
            offset = offset - self.start_offset
            return (await self.available_tracks())[offset]

    async def selected_track(self):
        return await self.track(await self.selected_offset())

    async def prev_track(self):
        return await self.next_track(reverse=True)

    async def next_track(self, reverse=False):
        direction = -1 if reverse else 1
        return await self.track(await self.selected_offset() + direction)

    async def select_track_key(self, key):
        for idx, track in enumerate(await self.available_tracks()):
            if track.key == key:
                await self.set_selected_offset(idx + self.start_offset)
                break

    def url_for_track(self, track):
        return self.plex_lib.build_url(track.Media[0].Part[0].key)

    async def allow_shuffle(self):
        info = await self.get_info()
        if info.get("allowShuffle", None) is None:
            if (await self.total_count()) == UNLIMITED:
                return False
            return True
        return info.allowShuffle

    @property
    def last_offset(self):
        if self.start_offset is None:
            return None
        return self.start_offset + len(self.info.Metadata) - 1

    async def more(self, after=True):
        if self.info is None:
            await self.get_info()
        url = URL(self.plex_lib.build_url(self.container_key))
        url = url.remove_query_params(["center", "includeBefore", "includeAfter"])
        args = {'includeAfter': 0, 'includeBefore': 0}
        if after:
            if self.last_offset >= (await self.total_count()) - 1:
                return
            args['includeAfter'] = 1
            t = await self.track(self.start_offset + (await self.available_count()) - 1)
            args['center'] = t.playQueueItemID
        else:
            if self.start_offset <= 1:
                return
            args['includeBefore'] = 1
            t = await self.track(self.start_offset)
            args['center'] = t.playQueueItemID
        url = url.include_query_params(**args)
        async with g.http.get(str(url), headers={"Accept": "application/json"}) as res:
            res.raise_for_status()
            info = DotMap((await res.json())['MediaContainer'])
            if after:
                self.info.Metadata += info.Metadata
                print(f"queue {self.container_key} append {len(info.Metadata)} items")
            else:
                self.info.Metadata = info.Metadata + self.info.Metadata
                print(f"queue {self.container_key} prepend {len(info.Metadata)} items")
                self.start_offset -= len(info.Metadata)

    async def available_tracks(self):
        info = await self.get_info()
        return info.Metadata

    async def available_count(self):
        return len(await self.available_tracks())

    async def total_count(self):
        info = await self.get_info()
        if not info.playQueueTotalCount:
            return UNLIMITED
        return info.playQueueTotalCount

    async def selected_item_id(self):
        info = await self.get_info()
        return info.playQueueSelectedItemID

    async def selected_offset(self):
        info = await self.get_info()
        return info.playQueueSelectedItemOffset

    async def get_track_info(self):
        track = await self.selected_track()
        return {
            'duration': track.duration,
            'key': track.key,
            'ratingKey': track.ratingKey,
            'containerKey': f"/playQueues/{self.info.playQueueID}",
            'playQueueID': self.info.playQueueID,
            'playQueueVersion': self.info.playQueueVersion,
            'playQueueItemID': track.playQueueItemID
        }
