from time import sleep
import json
import network
import machine
import ubinascii
import sys
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

class Context:
    _client = None
    topic = ""
    message = ""
    restart_requested = False
    def __init__(self, client, topic, message) -> None:
        self._client = client
        self.topic = topic
        self.message = message


# def publish_context(context: Context):
#     mqtt.publish(context.topic, json.dumps(context.message), retain=True, qos=1)

class Machine:
    def init(self, context: Context):
        return self.executing
    
    def executing(self, context: Context):
        return self.successful

    def successful(self, context: Context):
        return None

    def failed(self, context: Context):
        return None

    # def done(self, context: Context):
    #     return None

class Restart(Machine):
    is_resuming = True
    def init(self, context: Context):
        self.is_resuming = False
        return self.executing

    def executing(self, context: Context):
        if self.is_resuming:
            return self.successful

        context.restart_requested = True
        # machine.reset()
        # The sleep should never return
        # sleep(10)
        # TODO: Add failure reason
        return self.successful


class SoftwareUpdate(Machine):
    def init(self, context: Context):
        return self.executing

    def executing(self, context: Context):
        return self.successful

    def successful(self, context: Context):
        return None
    
class SoftwareList(Machine):
    def successful(self, context: Context):
        context.message["currentSoftwareList"] = [
            {"type": "", "modules":[
                {"name": __APPLICATION_NAME__, "version": __APPLICATION_VERSION__},
            ]}
        ]
        return super().successful(context)

class Unsupported(Machine):
    def init(self, context: Context):
        return self.failed

def run(client, context: Context, state_machine: Machine):
    try:
        print(f"Starting state machine: {type(state_machine).__name__}, context={context.message}")
        state = getattr(state_machine, context.message.get("status", "init"), None)
        error_count = 0
        while state is not None:
            try:
                print(f"Running state: {state.__name__}")
                state = state(context)
                if state is not None:
                    context.message["status"] = state.__name__
                    print(f"Save next state: {state.__name__}")

                    # NOTE: reduce the stack a bit to avoid exceeding it
                    # qos=1 causes problems with the stack limit as well (probably because it is blocking from the callback?)
                    client.publish(context.topic, json.dumps(context.message), retain=True, qos=0)

                    if context.restart_requested:
                        # TODO: does this message work to see if the message has been saved or not?
                        for _ in range(0, 5):
                            client.wait_msg()
                            sleep(0.5)
                        print("Restarting")
                        machine.reset()
                        # Should never happen?
                        sleep(60)
            except Exception as ex:
                
                print(f"Exception during state: {ex}")
                sys.print_exception(ex)
                if error_count > 0:
                    print("Aborting cyclic error")
                    break
                state = state_machine.failed
                error_count += 1

        print("Machine is finished")
    except Exception as ex:
        print(f"Machine exception. {ex}")

def get_machine(context: Context) -> Machine:
    command_type = context.topic.decode("utf-8").split("/")[-2]

    status = context.message.get("status")
    if status in ["successful", "failed"]:
        # Don't start at an already finished state to avoid recursive loops
        return None

    if command_type == "software_update":
        return SoftwareUpdate()
    if command_type == "software_list":
        return SoftwareList()
    if command_type == "restart":
        return Restart()
    return Unsupported()

def on_message(topic, msg):
    if not len(msg):
        return

    global is_restarting
    global led
    global mqtt

    if is_restarting:
        print("Waiting for device to restart")
        return

    command_type = topic.decode("utf-8").split("/")[-2]
    try:
        message = json.loads(msg.decode("utf-8"))
    except Exception as ex:
        print(f"Ignoring invalid json message: {msg.decode('utf-8')}")
        mqtt.publish(topic, json.dumps({"status":"failed","reason":"invalid message format"}), retain=True, qos=0)
        return
    
    if not isinstance(message, dict):
        print(f"Ignoring message as it is not a dictionary: {message}")
        mqtt.publish(topic, json.dumps({"status":"failed","reason":"invalid message format"}), retain=True, qos=0)
        return

    print(f"Received command: type={command_type}, topic={topic}, message={message}")

    context = Context(mqtt, topic, message)
    state_machine = get_machine(context)

    if state_machine is None:
        return

    run(mqtt, context, state_machine)    
    return


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
