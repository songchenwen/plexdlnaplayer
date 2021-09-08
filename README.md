# Plex DLNA Player

There is no built in way to cast plex music to DLNA speakers.
This project will be the bridge.

## Features

- Use UPNP auto discovery to find your DLNA devices in LAN.
- Use Plex GDM to notify Plex clients about the DLNA devices.
- Connect your DLNA device to plex.tv and let the Plex clients which don't support GDM find your DLNA devices. eg, Plexamp.
- Connect to your DLNA speakers with your Plex client's `Select Player` window.

## Installation
Just clone this repo and run `main.py` with python.
It's tested with python 3.9.

~~~
git clone https://github.com/songchenwen/plexdlnaplayer.git
cd plexdlnaplayer
python3 main.py
~~~

## Docker

It's recommended to use this project in [docker](https://github.com/users/songchenwen/packages/container/package/plexdlnaplayer).

*Note: It can only run with `host` network mode, due to udp broadcasting by UPNP and Plex GDM discovery.*

~~~
docker run -d \
  --name=plexdlnaplayer \
  --network host \
  --restart unless-stopped \
  -v <path to data>:/config
  ghcr.io/songchenwen/plexdlnaplayer
~~~

## Configuration

This project is configured with [pydantic settings](https://pydantic-docs.helpmanual.io/usage/settings/).


#### Environment Variables

| Env         |      Description      |         Default         |
| :---------- | :-------------------- | :---------------------- | 
| HTTP_PORT   | The port for the http server  | 32488 |
| HOST_IP     | IP of this host. Plex client will use `http://HOST_IP:HTTP_PORT` to connect to your DLNA devices | Auto Guess |
| ALIASES     | Preferred DLNA device names, looks like this `uuid:name1,ip:name2,origin_name:name3` | Empty |
| LOCATION_URL | The location url of your DLNA device. Setting this env will disable DLNA device auto discovery | None |
| CONFIG_PATH | In where to store the persistent data. | `/config`  |

Normally, you don't need to configure any of these environment variables.

#### Data Persistence

If you need data persistence with docker, you need to map `/config` to some location in your host. Data persistence will only be needed if you use the following features.

- Use Plexamp as controller instead of Plex.
- Edit device alias in the web page, instead of using environment variables.

#### Web Configuration

Go to `http://HOST_IP:HTTP_PORT` to manage your DLNA devices. 
In this page you can link your DLNA devices to your plex.tv account and edit the display name of them.

Because Plexamp don't support GDM discovery. You need to link your device to your account to use Plexamp as the controller. 

Yeah, I know, Plexamp has the better play queue support.

## Details

Any discovery of a new compatible DLNA device will start a new thread looping for its status.

Plex client uses the new subscribing method to get the player's status, while Plexamp uses the old inefficient polling way. In this case, using Plexamp with this project will certainly consume more resources.

DLNA devices can vary in functions. These differences will affect us most on the `auto next` part, which is where one track ends and we auto start playing the next track. If you find your device is unable to auto start the next track, please try to edit the `check_auto_next` function in `plex/adapters.py`. Pull request is always welcome.

## TODO

- [ ] A virtual device to play music with all the available DLNA speakers in sync.
