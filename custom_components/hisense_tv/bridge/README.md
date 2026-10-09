# Dynamic MQTT bridge for Hisense/VIDAA TVs (VIDAA 9)

Der klassische Mosquitto-Bridge mit **statischen** Zugangsdaten und den alten
Client-Zertifikaten funktioniert nach dem Update eines TVs auf VIDAA 9
(Firmware `V0000.09.xx`, `transport_protocol` >= 3000) nicht mehr. Der TV
lehnt den Verbindungsaufbau mit „App nicht kompatibel“ / CONNACK-Rückgabecode
`5` (not authorized) ab, weil neuere Firmware ein **zeitstempelbasiertes**
Login verlangt.

Dieser Dienst ersetzt den Mosquitto-Bridge: Er verbindet sich direkt mit dem
MQTT-Broker des TVs (Port 36669, TLS + Client-Zertifikat), authentifiziert
sich mit frisch generierten dynamischen Zugangsdaten und spiegelt die
`/remoteapp/#`-Topics in den Home-Assistant-Broker – identisch zu den Prefixen
der bisherigen Mosquitto-Konfiguration. Er liegt seit Version 0.3.09 direkt in
der Integration (`custom_components/hisense_tv/bridge/`) und wird von HACS
mitinstalliert.

## Voraussetzungen

1. **Frische Client-Zertifikate.** Die alten Keyfiles (z. B. von
   `d3nd3/Hisense-mqtt-keyfiles`) werden auf 09.x-Firmware abgelehnt. Zertifikat
   + privaten Schlüssel aus der aktuellen RemoteNOW- bzw. Vidaa-App extrahieren.
   Das mitgelieferte Skript macht das automatisch (Quelle: lokales APK,
   APKMirror-Link oder per `adb` das eigene Handy):

   ```
   python3 custom_components/hisense_tv/bridge/extract_certs.py --apkmirror "https://www.apkmirror.com/apk/v-america-operations-inc/vidaa-smart-tv/vidaa-smart-tv-1-09-06-002-3-release/" -o bridge/certs
   python3 custom_components/hisense_tv/bridge/extract_certs.py --adb -o bridge/certs   # App vom Handy ziehen
   # Ergebnis: bridge/certs/vidaa_client.pem + vidaa_client.key
   ```

   (Ohne `-o` schreibt das Skript nach `certs/` im aktuellen Verzeichnis.)

   Das Skript scandelt alle `.p12`-Keystores (verschachtelte APK-Bundles werden
   automatisch entpackt), öffnet den Kunden-Keystore
   (`res/raw/client_mobile_android.p12` bzw. `assets/client_mobile_android.p12`
   bzw. `res/3R.p12`, `CN=VidaaAppAndroidV01`) und stellt sicher, dass Zertifikat
   und Schlüssel zusammenpassen. Das Keystore-Passwort ist in der App hartkodiert
   und öffentlich bekannt (`186e990688070325a1c4b0ce275d2388`); Herleitung und
   Zertifikatsdetails dokumentiert die Protokollanalyse im
   [pyvidaa-Repository (VIDAA_PROTOCOL_ANALYSIS.md)](https://github.com/warrenrees/pyvidaa/blob/master/VIDAA_PROTOCOL_ANALYSIS.md).
   Manuell geht es auch – wegen RC2-Legacy-Ciphern ist unter OpenSSL 3.x
   `-legacy` Pflicht:

   ```
   openssl pkcs12 -legacy -in client_mobile_android.p12 -clcerts -nokeys -passin pass:186e990688070325a1c4b0ce275d2388 -out vidaa_client.pem
   openssl pkcs12 -legacy -in client_mobile_android.p12 -nocerts -nodes -passin pass:186e990688070325a1c4b0ce275d2388 -out vidaa_client.key
   ```

2. **TV-Uhr muss stimmen** (Zeitzone/DST): Die Zugangsdaten sind
   zeitstempelbasiert, bei Uhr-Differenz lehnt der TV ab.

3. Den bestehenden Mosquitto-Bridge zum TV entfernen bzw. deaktivieren,
   sonst kämpfen zwei Verbindungen um denselben `client_id`.

## Aktivierung in Home Assistant (empfohlen)

Seit 0.3.09 startet die Integration den Bridge selbst als überwachten Prozess:

1. Zertifikate nach `/config/certs/` legen (`vidaa_client.pem` + `.key`).
2. Integration -> *Bearbeiten* (Optionen) -> Schritt **„Bridge"** aktivieren,
   TV-IP, Zertifikatspfade, ggf. MAC/Brand eintragen, speichern.
3. Die Integration schreibt die Konfiguration nach
   `/config/hisense_bridge/config.yaml` und startet den Daemon automatisch.
   Log: `/config/hisense_bridge/bridge.log` (Neustart bei Absturz automatisch).

## Manueller Betrieb (alternativ)

Auf dem HA-Host (Abhängigkeiten sind über das Integrations-manifest
`paho-mqtt`/`PyYAML` bereits installiert):

```
cp custom_components/hisense_tv/bridge/config.example.yaml bridge/config.yaml   # anpassen
python3 -m custom_components.hisense_tv.bridge.bridge -c bridge/config.yaml -v
```

Für einen dauerhaften Betrieb z. B. als systemd-Unit oder Docker-Container auf
dem HA-Host einrichten. Der Dienst benötigt Netzwerkzugriff auf den TV
(Port 36669) und den HA-MQTT-Broker (Port 1883).

## Konfiguration

Siehe `config.example.yaml`. Die wichtigsten Felder:

| Feld | Bedeutung |
|------|-----------|
| `tvs[].host` | IP-Adresse des TVs |
| `tvs[].certfile` / `keyfile` | mTLS-Client-Zertifikat (siehe oben) |
| `tvs[].prefix_in` | MQTT-In-Prefix der HA-Integration („hisense“) |
| `tvs[].prefix_out` | MQTT-Out-Prefix der HA-Integration („hisense“) |
| `tvs[].topic_client_id` | Client-ID, die die Integration in den Topics nutzt (`HomeAssistant`) |
| `tvs[].auth_mode` | `auto` (empfohlen), `static` oder `dynamic` |

MAC und Brand werden automatisch aus dem UPnP-Deskriptor des TVs gelesen
(`/MediaServer/rendererdevicedesc.xml`); explizite Werte in der Config gewinnen.

## Topic-Mapping

Wie bisher: HA-Topics `hisense/remoteapp/...` <-> TV-Topics `/remoteapp/...`.
Der Bridge abonniert auf dem HA-Broker `prefix_out/remoteapp/tv/#` und
publiziert alle TV-Nachrichten unter `prefix_in/remoteapp/#`. Falls sich die
dynamische Client-ID (MAC-abgeleitet) von der Topic-Client-ID der Integration
unterscheidet, wird nur diese ID im Topic umgeschrieben – beides bleibt für
die HA-Integration transparent.

## Pairing / PIN

Das PIN-Pairing läuft unverändert über die Home-Assistant-Integration: Beim
Setup/Reauth löst sie `vidaa_app_connect` aus, der TV zeigt eine PIN, die in
HA eingegeben wird. Nach dem Firmware-Update einmal erneut pairen (alte
Session des TVs wird mit dem neuen `client_id` nicht mehr akzeptiert).

VIDAA-9-Details, die beim Pairing zwingend sind (seit 0.3.11 in der Integration
korrekt umgesetzt):

- Die PIN wird als **Integer** gesendet (`{"authNum": 1234}`). Als String
  antwortet der TV mit `result:100 "illegal authNum!!"`.
- Der Token wird über `/remoteapp/tv/platform_service/{cid}/data/gettoken`
  angefragt (nicht `/actions/`); erst nach Ausstellung des `accesstoken`
  (2 Tage gültig) gibt der TV-Broker die Daten-Topics frei.
- **Keine Wildcard-Subscriptions:** VIDAA 9 lehnt `…#`-Abos ab. Der Bridge
  abonniert deshalb die exakten Antwort-/Broadcast-Topics und erneuert sie
  periodisch, damit die Freigaben nach dem Pairing ankommen. Ältere Firmware
  gewährt dieselben exakten Topics sofort – der Bridge bleibt abwärtskompatibel.

## Troubleshooting

- **„App nicht kompatibel“ / CONNACK 5:** Old Static-Creds bzw. alte Zertifikate
  – frische Zertifikate aus der aktuellen App verwenden, `auth_mode` prüfen.
- **PIN abgelehnt (`illegal authNum!!`):** veraltete Integration (< 0.3.11)
  schickt die PIN als String; Upgrade auf 0.3.11 oder PIN manuell als Integer.
- **Keine TV-Antworten trotz Verbindung:** Wildcard-Abos werden auf VIDAA 9
  verweigert – genau-zugeschnittene Topics verwenden (Bridge 0.3.11+) und nach
  dem PIN-Pairing ggf. 45 s für das erneute Subscribe warten.
- **TV läuft, aber der Bridge reconnectet endlos:** TV-Uhr prüfen
  (Zeitzone/DST), MAC-Vergleich: der Bridge muss dieselbe MAC verwenden, die
  der TV selbst im Deskriptor meldet (Groß-/Kleinschreibung ändert die
  abgeleitete Client-ID!).
- **WLAN- vs. Ethernet-MAC:** Das TV-`mac`-Feld im Deskriptor (für die
  dynamische Auth) ist oft die **Ethernet-MAC** und unterscheidet sich von der
  **WLAN-MAC**, die HA für Wake-on-LAN nutzt (Entry-`CONF_MAC`). Beides nicht
  verwechseln: Bridge-Feld `mac` leer lassen (= Auto-Erkennung aus dem
  Deskriptor) oder explizit auf die Deskriptor-MAC setzen; WoL läuft
  unabhängig über die Entry-MAC weiter.
- **Logs (integriert):** `/config/hisense_bridge/bridge.log`;
  manuell: `python3 -m custom_components.hisense_tv.bridge.bridge -c bridge/config.yaml -v`
  zeigt transport_protocol, gewählte Auth-Methode und CONNACK-Codes.
- **Transport-Protokoll prüfen:** `curl http://<IP>:38400/MediaServer/rendererdevicedesc.xml`
  (alternativ Port 18400). `transport_protocol < 3000` = Static Auth,
  `>= 3000` = dynamische Auth.

## Lizenz/Anerkennung

Der Credential-Algorithmus wurde aus der offiziellen Vidaa-App
(`libmqttcrypt.so`) reverse-engineert, Referenzimplementierung:
[warrenrees/pyvidaa](https://github.com/warrenrees/pyvidaa) (MIT).