from pydantic import BaseSettings
from pathlib import Path
import json


class Settings(BaseSettings):
    http_port = 32488
    host_ip: str = None
    product = "Plex DLNA Player"
    aliases: str = ""
    location_url: str = None
    version = "1"
    platform = "Linux"
    platform_version = "1"
    plex_notify_interval = 0.5
    config_path = "config"
    data_file_name = "data.json"

    def dlna_name_alias(self, uuid: str, name: str, ip: str):
        data = self.load_data()
        alias = data.get(uuid, {}).get('alias', None)
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

    def save_dlna_name_alias(self, uuid, alias):
        data = self.load_data()
        info = data.get(uuid, {})
        info['alias'] = alias
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

    def get_token_for_uuid(self, uuid):
        d = self.load_data()
        return d.get(uuid, {}).get("token", None)

    def set_token_for_uuid(self, uuid, token):
        d = self.load_data()
        info = d.get(uuid, {})
        info['token'] = token
        d[uuid] = info
        self.save_data(d)


settings = Settings()
