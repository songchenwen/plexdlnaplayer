from pydantic import BaseSettings
from pathlib import Path
import json


class Settings(BaseSettings):
    http_port = 32488
    host_ip: str = None
    product = "Plex DLNA Player"
    aliases: str = ""
    # Only register these renderers, as `uuid,name,ip` - empty means all.
    only_devices: str = ""
    # Never register these renderers, same format. Applied after only_devices.
    ignore_devices: str = ""
    location_url: str = None
    version = "1"
    platform = "Linux"
    platform_version = "1"
    plex_notify_interval = 0.5
    # Rewrite the Plex server's https plex.direct URL to a plain http LAN URL, for
    # renderers that cannot fetch TLS. Off by default.
    force_http = False
    # Address to use instead of the plex.direct hostname when force_http is on.
    # Required when the controller reaches Plex over IPv6, since the IPv6 form of
    # a plex.direct name cannot be turned into a usable LAN address on its own.
    plex_lan_address: str = None
    config_path = "config"
    data_file_name = "data.json"

    def dlna_name_alias(self, uuid: str, name: str, ip: str):
        data = self.load_data()
        alias = data.get(uuid, {}).get("alias", None)
        if alias is not None:
            return alias
        if not settings.aliases:
            return name
        aliases = settings.aliases.split(",")
        for alias in aliases:
            k, v = alias.split(":")
            if k.strip() in [uuid.strip(), name.strip(), ip.strip()]:
                return v.strip()
        return name

    def device_allowed(self, uuid: str, name: str, ip: str):
        """Whether a discovered renderer should be registered.

        Matched the same way as aliases: against the uuid, the name or the ip.
        """
        keys = {(uuid or "").strip(), (name or "").strip(), (ip or "").strip()}

        def listed(raw):
            return {k.strip() for k in raw.split(",") if k.strip()} & keys

        if self.only_devices and not listed(self.only_devices):
            return False
        if self.ignore_devices and listed(self.ignore_devices):
            return False
        return True

    def save_dlna_name_alias(self, uuid, alias):
        data = self.load_data()
        info = data.get(uuid, {})
        info["alias"] = alias
        data[uuid] = info
        self.save_data(data)

    def load_data(self):
        p = Path(self.config_path).joinpath(self.data_file_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            return {}
        try:
            with open(p) as f:
                j = json.load(f)
                return j
        except Exception:
            return {}

    def save_data(self, data):
        p = Path(self.config_path).joinpath(self.data_file_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch()
        with open(p, mode="w") as f:
            json.dump(data, f, indent=4)

    def remember_device(self, uuid, name, location_url):
        """Record a renderer that registered successfully, so a later start can
        go straight to its description URL instead of waiting on discovery.

        Discovery only learns about a renderer when it answers an M-SEARCH or
        announces itself. A renderer that is asleep, or that simply misses the
        search, is invisible until the next sweep, which is why the first play
        after a cold start often has nothing to talk to.
        """
        data = self.load_data()
        info = data.get(uuid, {})
        known = info.get("known_device", {})
        if known.get("location_url") == location_url and known.get("name") == name:
            return
        info["known_device"] = {"name": name, "location_url": location_url}
        data[uuid] = info
        self.save_data(data)

    def known_device_urls(self):
        """Description URLs of every renderer that has registered before."""
        urls = []
        for info in self.load_data().values():
            if not isinstance(info, dict):
                continue
            url = (info.get("known_device") or {}).get("location_url")
            if url and url not in urls:
                urls.append(url)
        return urls

    def get_token_for_uuid(self, uuid):
        d = self.load_data()
        return d.get(uuid, {}).get("token", None)

    def set_token_for_uuid(self, uuid, token):
        d = self.load_data()
        info = d.get(uuid, {})
        info["token"] = token
        d[uuid] = info
        self.save_data(d)


settings = Settings()
