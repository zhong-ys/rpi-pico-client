## Getting started

Install dependencies by running the following on the device's python console:

```python
# Setup network first
import network
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect("yourssid", "password")

# Then install the required packages
import mip
mip.install("umqtt.simple")
mip.install("umqtt.robust")
```

## Setting up the thin-edge.io device

Since the Raspberry Pi Pico needs to communicate with thin-edge.io over the network, the thin-edge.io instance needs to be setup to enable this communication.

1. On the main device where the MQTT broker, tedge-mapper-c8y and tedge-agent, run the following commands to set the configuration:

    ```sh
    tedge config set c8y.proxy.bind.address 0.0.0.0
    tedge config set mqtt.bind.address 0.0.0.0
    tedge config set http.bind.address 0.0.0.0

    tedge config set c8y.proxy.client.host $HOST.local
    tedge config set http.client.host $HOST.local
    ```

    If your `$HOST` variable is not set, then replace it with the actual name of the host your are running on (e.g. the address must be reachable from outside of the device)

2. Restart the services

    ```
    systemctl restart tedge-agent
    tedge reconnect c8y
    ```

3. Check that the address is reachable from outside, e.g.

    ```sh
    curl http://$HOST.local:8001/c8y/inventory/managedObjects/
    ```

## Installing the client on the Raspberry Pi Pico

1. Using Thonny, connect to your Raspberry Pi Pico

2. Copy across the following files

    * `src/main.py` => `/main.py`
    * `src/config.py` => `/config.py`

3. Edit the `config.py` and update WIFI and broker settings

4. Start the application (ideally without Thonny connected, otherwise Thonny can interfere with functionality such as device restart, software updates etc.)
 
## References and Ideas

OTA updates via:
    * https://pypi.org/project/micropython-ota/
