import asyncio
import unittest

from dotmap import DotMap

import plex.adapters as pa  # noqa: F401  (import first; dlna imports it back)
import dlna.dlna_device as dd


class WaiterWakeTest(unittest.TestCase):
    """`continue` inside the field loop continued that loop, not the outer one.

    A waiter interested in two fields that both changed was appended to the
    removal list twice, and the second list.remove() raised ValueError out of a
    task nothing awaits. Timeline waiters then leaked.
    """

    def _adapter(self, waiters):
        a = pa.PlexDlnaAdapter.__new__(pa.PlexDlnaAdapter)
        a.wait_state_change_events = waiters
        return a

    def _waiter(self, fields):
        return dict(event=asyncio.Event(), interesting_fields=fields)

    def test_a_waiter_matching_two_changed_fields_is_removed_once(self):
        w = self._waiter(["state", "elapsed"])
        a = self._adapter([w])
        changed = DotMap({"state": "PLAYING", "elapsed": 1000, "old": {"elapsed": 0}})
        asyncio.run(a.state_changed(changed))
        self.assertTrue(w["event"].is_set())
        self.assertEqual(a.wait_state_change_events, [])

    def test_a_waiter_for_an_unchanged_field_is_left_alone(self):
        w = self._waiter(["volume"])
        a = self._adapter([w])
        asyncio.run(a.state_changed(DotMap({"state": "PLAYING", "old": {}})))
        self.assertFalse(w["event"].is_set())
        self.assertEqual(len(a.wait_state_change_events), 1)

    def test_a_waiter_with_no_fields_wakes_on_anything(self):
        w = self._waiter(None)
        a = self._adapter([w])
        asyncio.run(a.state_changed(DotMap({"state": "PLAYING", "old": {}})))
        self.assertTrue(w["event"].is_set())

    def test_an_elapsed_jump_wakes_its_waiter(self):
        w = self._waiter(["elapsed_jump"])
        a = self._adapter([w])
        asyncio.run(a.state_changed(DotMap({"elapsed": 90000, "old": {"elapsed": 1000}})))
        self.assertTrue(w["event"].is_set())


class RemoveSelfTest(unittest.TestCase):
    """Removal is rescheduled on every failure past the threshold.

    devices.remove() then raised ValueError on the second call, half way
    through tearing the device down.
    """

    class FakeState:
        def __init__(self, has_event):
            self.state = None
            self.looping_wait_event = asyncio.Event() if has_event else None
            self._thread_should_stop = False

    def _device(self, has_event=True):
        d = dd.DlnaDevice.__new__(dd.DlnaDevice)
        d.uuid = "u1"
        d.name = "Fake"
        d.stop_subscribe = lambda service_type=None: None
        state = self.FakeState(has_event)
        adapter = type("A", (), {"state": state, "queue": object()})()
        d._adapter = adapter
        return d, adapter

    def _run(self, d, adapter):
        import plex.adapters as real_adapters
        import plex.subscribe as real_sub
        orig_by_device = real_adapters.adapter_by_device
        orig_remove = real_adapters.remove_adapter
        orig_notify_d = real_sub.sub_man.notify_device_disconnected
        orig_notify_s = real_sub.sub_man.notify_server_device

        async def noop(*a, **k):
            return None

        real_adapters.adapter_by_device = lambda dev, query_params=None: adapter
        real_adapters.remove_adapter = lambda a: None
        real_sub.sub_man.notify_device_disconnected = noop
        real_sub.sub_man.notify_server_device = noop
        try:
            asyncio.run(d.remove_self())
        finally:
            real_adapters.adapter_by_device = orig_by_device
            real_adapters.remove_adapter = orig_remove
            real_sub.sub_man.notify_device_disconnected = orig_notify_d
            real_sub.sub_man.notify_server_device = orig_notify_s

    def test_removing_twice_does_not_raise(self):
        d, adapter = self._device()
        dd.devices.append(d)
        try:
            self._run(d, adapter)
            self._run(d, adapter)  # would raise ValueError before
        finally:
            if d in dd.devices:
                dd.devices.remove(d)
        self.assertNotIn(d, dd.devices)

    def test_a_device_removed_before_its_state_loop_started(self):
        """looping_wait_event is None until the loop runs."""
        d, adapter = self._device(has_event=False)
        dd.devices.append(d)
        try:
            self._run(d, adapter)
        finally:
            if d in dd.devices:
                dd.devices.remove(d)
        self.assertNotIn(d, dd.devices)


if __name__ == "__main__":
    unittest.main()
