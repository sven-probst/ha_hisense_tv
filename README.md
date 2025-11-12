# Hisense TV Integration for Home Assistant
# This is a fork from sehaas/ha_hisense_tv and V4n1X/ha_hisense_tv, with some merged changes and PRs.

Integration for an Hisense TV as media player into Home Assistant. The communication is handled via the integrated MQTT broker and wake-on-LAN.
Requires Home Assistant >= `2021.12.x`.

## Current features:
* Turn on / off
* Display current status
  * Source (TV, HDMI, Apps)
  * Channel name / number
  * EPG data of current show
* Volume control
* Media browser
  * LNB selector
  * Channel selector
  * Apps
* send keys as single or as sequence from a array via action
* send touchpad dx/dy-values
* autodetect TV
* touchpad support
* compatibility-layer to webos-tv (choose this TV type while using https://github.com/Nerwyn/universal-remote-card )

TBD:
* Read picture setting
* Expose all keys as buttons
* Enhance EPG/guide handling

## Configuration

The TV provides a MQTT broker on port `36669`. Home Assistant can only communicate with one MQTT broker, so you have to create a bridge between the two broker.

## MQTT

The MQTT broker is secured by credentials. Some TVs (like mine) even require client certificates for incomming connections. I won't include them in this repo, but you can find them online or extract them yourself. See [Acknowledgment](https://github.com/sehaas/ha_hisense_tv#acknowledgment).

Connection shema:
```
+-----------+          +-----------+
| Home      |  client  | Mosquitto |
| Assistant |--------->|           |
+-----------+          +-----------+
                            /\
                     bridge ||
                            \/
                      +-------------+
                      | Hisense TV  |
                      | MQTT Broker |
                      +-------------+
```

The `mosquitto` bridge configuration using client certificates.

```
connection hisense
address <TV_IP_ADDRESS>:36669
username <HISENSE_MQTT_USERNAME>
password  <HISENSE_MQTT_PASSWORD>
clientid HomeAssistant
bridge_tls_version tlsv1.2
bridge_cafile hisense_ca.pem
bridge_certfile hisense_client.pem
bridge_keyfile hisense_client.key
bridge_insecure true
start_type automatic
try_private true
topic /remoteapp/# both 0 <MQTT_PREFIX> ""
```
Replace `<TV_IP_ADDRESS>`, credentials and `<MQTT_PREFIX>` according to your setup. The `<MQTT_PREFIX>` is needed if you have multiple TVs, otherwise you should just use the default `hisense`:
```
topic /remoteapp/# both 0 hisense ""
```

(Optional) If you have multiple TVs you have to replicate the whole configuration for each TV.
The `<MQTT_PREFIX>` must be unique for every TV. For example:
```
topic /remoteapp/# both 0 livingroom_tv ""
```
```
topic /remoteapp/# both 0 kids_tv ""
```

(Optional) This setup uses the same prefix for incoming and outgoing messages. The integration supports separated values. You have to adapt the topic setup accordingly.

## Wake-on-LAN

The TV can be turned on by a Wake-on-LAN packet. The MAC address must be configured during integration setup.

## Setup in Home Assistant

The integration can be added via the Home Assistant UI. Add the integration and setup your TV. During the first setup your TV should be turned on. The integration requires a PIN code from you TV. The PIN will be triggered automatically during setup. This is a onetime step where the client `HomeAssistant` is requesting access to remote controll the TV.

## Using with https://github.com/Nerwyn/universal-remote-card

working example (please change entities to your control- and power-entity)

~~~
type: custom:universal-remote-card
platform: LG webOS
entity: media_player.wohnzimmer_tv_steuerung
rows:
  - - power
    - volume_mute
    - language
  - - menu
    - home
  - - - exit
      - guide
    - navigation_buttons
    - - channel_up
      - channel_down
  - - back
  - - red
    - green
    - yellow
    - blue
  - - youtube
    - launch_browser
    - keyboard
    - textbox
  - - rewind
    - fast_forward
    - play
    - pause
    - stop
  - - monitor_on
    - monitor_off
    - fm_radio
  - touchpad
media_player_id: media_player.wohnzimmer_tv_steuerung
custom_actions:
  - type: button
    name: launch_browser
    icon: mdi:web
    tap_action:
      action: perform-action
      data:
        app_name: TV Browser
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
      perform_action: hisense_tv.launch_app
  - type: button
    name: monitor_off
    icon: mdi:monitor-off
    tap_action:
      action: perform-action
      data:
        key:
          - EXIT
          - EXIT
          - MENU
          - UP
          - UP
          - OK
      perform_action: hisense_tv.send_key
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
  - type: button
    name: monitor_on
    icon: mdi:monitor
    tap_action:
      action: perform-action
      data:
        key:
          - EXIT
          - EXIT
      perform_action: hisense_tv.send_key
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
  - type: button
    name: fm_radio
    icon: mdi:radio
    tap_action:
      action: perform-action
      data:
        key:
          - EXIT
          - 6
          - 1
          - EXIT
          - EXIT
          - MENU
          - UP
          - UP
          - OK
      perform_action: hisense_tv.send_key
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
  - type: button
    name: language
    icon: mdi:translate
    tap_action:
      action: perform-action
      data:
        key: LANG
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
      perform_action: hisense_tv.send_key
  - type: touchpad
    name: touchpad
    tap_action:
      action: perform-action
      data:
        key: OK
      perform_action: hisense_tv.send_key
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
    up:
      tap_action:
        action: key
        key: up
      hold_action:
        action: repeat
      type: button
      styles: ""
    down:
      tap_action:
        action: key
        key: down
      hold_action:
        action: repeat
      type: button
    left:
      tap_action:
        action: key
        key: left
      hold_action:
        action: repeat
      type: button
    right:
      tap_action:
        action: key
        key: right
      hold_action:
        action: repeat
      type: button
    drag_action:
      action: perform-action
      perform_action: hisense_tv.send_mouse_event
      data:
        dx: |
          {{ deltaX }}
        dy: |
          {{ deltaY }}
      target:
        entity_id: media_player.wohnzimmer_tv_steuerung
      repeat_delay: 1
    styles: |-
      :host {
        top: 50%;
        left: 50%;
        width: 80%;
        height: 80%;
      }
      toucharea {
       border-radius: 10px;
       border: 1px solid #444;
      }
    hold_action:
      action: keyboard
      media_player_id: media_player.wohnzimmer_tv_steuerung
styles: |-
  #green::part(icon) {
    color: green;
  }
  #red::part(icon) {
    color: red;
  }
  #blue::part(icon) {
    color: blue;
  }
  #yellow::part(icon) {
    color: yellow;
  }
  #power::part(icon) {
    color: {% if  is_state('switch.wohnzimmer_tv_power','on') %}
                red
           {% else %}
                blue
           {% endif %};
  }
~~~

# YMMV

Tested on an [Toshiba 40LV3E63DG](https://toshiba-tv.com/de-de/smart-tvs/40lv3e63dg) with mandatory client certificates. `gettvstate` does not return a `state` but can be used to authenticate the client.


# Acknowledgment
Everything I needed to write this integration could be gathered from these sources. Information about the MQTT topics, credentials or certificates can be found there.

* [@Krazy998's mqtt-hisensetv](https://github.com/Krazy998/mqtt-hisensetv)
* [@newAM's hisensetv_hass](https://github.com/newAM/hisensetv_hass)
* [HA Community](https://community.home-assistant.io/t/hisense-tv-control/97638/1)
* [RemoteNOW App](https://play.google.com/store/apps/details?id=com.universal.remote.ms)
* [@d3nd3](https://github.com/d3nd3/Hisense-mqtt-keyfiles)

# Installation

Download the package put it in /config/custom_componennts
HA->settings->intergrations->hisense_tv

"The integration can be added via the Home Assistant UI. Add the integration and setup your TV. During the first setup your TV should be turned on. The integration requires a PIN code from you TV. The PIN will be triggered automatically during setup. This is a onetime step where the client `HomeAssistant` is requesting access to remote controll the TV." <-- 

still editing<-----


