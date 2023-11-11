from time import sleep
import json
import network
import machine
import ubinascii
from machine import Timer
import config

# install library if not present
try:
    from umqtt.robust import MQTTClient
except ImportError:
    print("Installing missing dependencies")
    import mip
    mip.install("umqtt.simple")
    mip.install("umqtt.robust")
    from umqtt.robust import MQTTClient

# Application info
__APPLICATION_NAME__ = "micropython-agent"
__APPLICATION_VERSION__ = "0.0.1"

# Device info
serial_no = ubinascii.hexlify(machine.unique_id()).decode()
device_id = f"rpi-pico-{serial_no}"
topic_identifier = f"te/device/{device_id}//"

# Device in/out
adcpin = 4
sensor = machine.ADC(adcpin)
led = machine.Pin("LED", machine.Pin.OUT)


# MQTT Client (used to connect to thin-edge.io)
mqtt_client = f"{topic_identifier}#{__APPLICATION_NAME__}"
mqtt = MQTTClient(mqtt_client, config.TEDGE_BROKER_HOST, int(config.TEDGE_BROKER_PORT) or 1883, ssl=False)
mqtt.DEBUG = True

# Last Will and Testament message in case of unexpected disconnects
mqtt.lw_topic = f"{topic_identifier}/e/disconnected"
mqtt.lw_msg = json.dumps({"text": "Disconnected"})
mqtt.lw_qos = 1
mqtt.lw_retain = False

is_restarting = False


def read_temperature():
    adc_value = sensor.read_u16()
    volt = (3.3/65535) * adc_value
    temperature = 27 - (volt - 0.706)/0.001721
    return round(temperature, 1)

def blink_led(times=6, rate=0.1):
    for i in range(0, times):
        led.toggle()
        sleep(rate)
    led.on()

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    while wlan.isconnected() == False:
        print('Waiting for connection..')
        sleep(1)
    ip = wlan.ifconfig()[0]
    print(f'Connected on {ip}')
    return ip

def on_message(topic, msg):
    print("Received command")
    if not len(msg):
        return

    global is_restarting
    global led
    global mqtt

    if is_restarting:
        print("Waiting for device to restart")
        return

    command_type = topic.decode("utf-8").split("/")[-2]
    message = json.loads(msg.decode("utf-8"))
    print(f"Received command: type={command_type}, topic={topic}, message={message}")

    if command_type == "restart":
        if message["status"] == "init":
            is_restarting = True
            led.off()
            state = {
                "status": "executing",
            }
            mqtt.publish(topic, json.dumps(state), retain=True, qos=1)
            print("Restarting device")

            # or machine.soft_reset()
            machine.reset()
            sleep(10)
        elif message["status"] == "executing":
            state = {
                "status": "successful",
            }
            mqtt.publish(topic, json.dumps(state), retain=True, qos=1)

    elif command_type == "firmware_update":
        print("Applying firmware update")
    elif command_type == "software_list":
        if message["status"] == "init":
            state = {
                "status": "successful",
                "currentSoftwareList": [
                    {"type": "", "modules":[
                        {"name": __APPLICATION_NAME__, "version": __APPLICATION_VERSION__},
                    ]}
                ]
            }
            mqtt.publish(topic, json.dumps(state), retain=True, qos=1)
    else:
        print("Unsupported command")

def publish_telemetry(client):
    # global mqtt
    message = {
        "temp": read_temperature(),
    }
    blink_led(2, 0.15)
    print(f"Publishing telemetry data. {message}")
    # NOTE: DO NOT USE QOS > 0, as this will block the client!
    client.publish(f"{topic_identifier}/m/environment", json.dumps(message), qos=0)


def mqtt_connect():
    print(f"Connecting to thin-edge.io broker: broker={mqtt.server}:{mqtt.port}, client_id={mqtt.client_id}")
    mqtt.connect()

    # wait for the broker to consider us dead
    sleep(6)
    mqtt.check_msg()

    mqtt.set_callback(on_message)
    print("Connected to thin-edge.io broker")

    # register device
    mqtt.publish(f"{topic_identifier}", json.dumps({
        "@type": "child-device",
        "name": device_id,
        "type": "micropython",
    }), qos=1, retain=True)

    # publish hardware info
    mqtt.publish(f"{topic_identifier}/twin/c8y_Hardware", json.dumps({
        "serialNumber": serial_no,
        "model": "Raspberry Pi Pico W",
        "revision": "RP2040",
    }), qos=1, retain=True)

    # register support for commands
    mqtt.publish(f"{topic_identifier}/cmd/restart", b"{}", retain=True, qos=1)
    mqtt.publish(f"{topic_identifier}/cmd/software_update", b"{}", retain=True, qos=1)
    mqtt.publish(f"{topic_identifier}/cmd/software_list", b"{}", retain=True, qos=1)

    # startup messages
    mqtt.publish(f"{topic_identifier}/e/boot", json.dumps({"text": f"Application started. version={__APPLICATION_VERSION__}"}), qos=1)

    # subscribe to commands
    mqtt.subscribe(f"{topic_identifier}/cmd/+/+")
    print("Subscribed to commands topic")

    # give visual queue that the device booted up
    blink_led()

    # TODO: make sure timer is shutdown on unexpected errors
    timer = Timer()
    timer.init(period=10000, mode=Timer.PERIODIC, callback=lambda t: publish_telemetry(mqtt))

    while 1:
        try:
            print("looping")
            blink_led(2, 0.5)
            mqtt.wait_msg()   # blocking
            #mqtt.check_msg()   # non-blocking
        except Exception as ex:
            print(f"Unexpected error: {ex}")


def main():
    print(f"Starting: device_id={device_id}, topic={topic_identifier}")
    connect_wifi()
    led.on()
    mqtt_connect()

if __name__ == "__main__":
    main()
