# from time import sleep

import json
import network
import machine
import sys
import time
import ubinascii
import asyncio
from machine import Timer
from primitives import Queue
from asyncio import sleep

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
    mip.install("github:peterhinch/micropython-async/v3/primitives")

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


def read_temperature():
    adc_value = sensor.read_u16()
    volt = (3.3/65535) * adc_value
    temperature = 27 - (volt - 0.706)/0.001721
    return round(temperature, 1)

async def blink_led_async(times=6, rate=0.1):
    for i in range(0, times):
        led.toggle()
        await sleep(rate)
    led.on()

def blink_led(times=6, rate=0.1):
    for i in range(0, times):
        led.toggle()
        time.sleep(rate)
    led.on()

async def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    while wlan.isconnected() == False:
        print('Waiting for connection..')
        await sleep(1)
    ip = wlan.ifconfig()[0]
    print(f'Connected on {ip}')
    return ip

#
# Commands
#
class Context:
    topic = ""
    type = ""
    message = ""
    reason = None
    restart_requested = False
    def __init__(self, topic, type, message) -> None:
        self.topic = topic
        self.type = type
        self.message = message

class Machine:
    def init(self, context: Context):
        return self.executing

    def executing(self, context: Context):
        return self.successful

    def successful(self, context: Context):
        return None

    def failed(self, context: Context):
        if not context.reason:
            context.reason = "Unknown failure"
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

        # TODO: skip actual restart for testing
        context.restart_requested = False
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

async def run(client, context: Context, state_machine: Machine):
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

                    client.publish(context.topic, json.dumps(context.message), retain=True, qos=1)
                    await sleep(1)

                    if context.restart_requested:
                        # TODO: does this message work to see if the message has been saved or not?
                        for _ in range(0, 5):
                            client.wait_msg()
                            await sleep(0.5)
                        print("Restarting")
                        machine.reset()
                        # Should never happen?
                        await sleep(60)
            except Exception as ex:
                print(f"Exception during state: {ex}")
                sys.print_exception(ex)
                if error_count > 0:
                    print("Aborting cyclic error")
                    break
                state = state_machine.failed
                if not context.reason:
                    context.reason = "Unknown failure"
                error_count += 1

        print("Machine is finished")
    except Exception as ex:
        print(f"Machine exception. {ex}")

def get_machine(context: Context) -> Machine:
    if context.type == "software_update":
        return SoftwareUpdate()
    if context.type == "software_list":
        return SoftwareList()
    if context.type == "restart":
        return Restart()
    return Unsupported()

async def command_executor(queue, client):
    count = 1
    print("Starting command executor")
    while True:
        topic, command_type, command = await queue.get()  # Blocks until data is ready
        print(f"[id={count}, type={command_type}] Processing command. {topic} {command}")

        context = Context(topic, command_type, command)
        state_machine = get_machine(context)
        if state_machine:
            await run(client, context, state_machine)

        print(f"[id={count}, type={command_type}] Finished command")
        count += 1

#
# Telemetry
#
def publish_telemetry(client, period=10):
    message = {
        "temp": read_temperature(),
    }
    print(f"Publishing telemetry data. {message}")
    blink_led(2, 0.15)
    try:
        client.publish(f"{topic_identifier}/m/environment", json.dumps(message), qos=0)
        client.check_msg()
    except Exception as ex:
        pass

#
# Agent
#
async def agent(queue, client):
    print(f"Connecting to thin-edge.io broker: broker={client.server}:{client.port}, client_id={client.client_id}")
    client.connect()

    # wait for the broker to consider us dead
    await sleep(2)
    client.check_msg()

    def _queue_message(topic, message):
        if len(message):
            try:
                command = json.loads(message.decode("utf-8"))
                if not isinstance(command, dict):
                    raise ValueError("payload is not a dictionary")
                command_type = topic.decode("utf-8").split("/")[-2]
                print(f"Add message to queue: {topic} {message}")

                # TODO: Keep track of which operations are already known to be in progress
                # or if it is resuming after a restart.
                command_status = command.get("status", "")
                # if command_status in ["successful", "failed"]:
                if command_status not in ["init"]:
                    raise ValueError(f"command is already in final state. {command_status}")

                queue.put_nowait((topic, command_type, command))
            except Exception as ex:
                print(f"Ignoring message. topic={topic}, message={message}, error={ex}")

    client.set_callback(_queue_message)
    print("Connected to thin-edge.io broker")

    # register device
    client.publish(f"{topic_identifier}", json.dumps({
        "@type": "child-device",
        "name": device_id,
        "type": "micropython",
    }), qos=1, retain=True)

    # publish hardware info
    client.publish(f"{topic_identifier}/twin/c8y_Hardware", json.dumps({
        "serialNumber": serial_no,
        "model": "Raspberry Pi Pico W",
        "revision": "RP2040",
    }), qos=1, retain=True)

    # register support for commands
    client.publish(f"{topic_identifier}/cmd/restart", b"{}", retain=True, qos=1)
    client.publish(f"{topic_identifier}/cmd/software_update", b"{}", retain=True, qos=1)
    client.publish(f"{topic_identifier}/cmd/software_list", b"{}", retain=True, qos=1)

    # startup messages
    client.publish(f"{topic_identifier}/e/boot", json.dumps({"text": f"Application started. version={__APPLICATION_VERSION__}"}), qos=1)

    # subscribe to commands
    client.subscribe(f"{topic_identifier}/cmd/+/+")
    print("Subscribed to commands topic")

    # give visual queue that the device booted up
    await blink_led_async()

    while True:
        try:
            await asyncio.sleep(2)
            client.wait_msg()   # blocking
        except Exception as ex:
            print(f"Unexpected error: {ex}")

#
# Main
#
async def main():
    try:
        queue = Queue(10)
        print(f"Starting: device_id={device_id}, topic={topic_identifier}")
        await connect_wifi()
        led.on()

        # MQTT Client (used to connect to thin-edge.io)
        client_id = f"{topic_identifier}#{__APPLICATION_NAME__}"
        mqtt = MQTTClient(
            client_id,
            config.TEDGE_BROKER_HOST,
            int(config.TEDGE_BROKER_PORT) or 1883,
            ssl=False,
        )
        mqtt.DEBUG = True

        # Last Will and Testament message in case of unexpected disconnects
        mqtt.lw_topic = f"{topic_identifier}/e/disconnected"
        mqtt.lw_msg = json.dumps({"text": "Disconnected"})
        mqtt.lw_qos = 1
        mqtt.lw_retain = False

        agent_task = asyncio.create_task(agent(queue, mqtt))
        command_task = asyncio.create_task(command_executor(queue, mqtt))

        await asyncio.sleep(0)

        # NOTE: use Timer over async tasks due to a blocking issue when publishing MQTT messages
        # This could be solved in the future if necessary
        timer = Timer()
        timer.init(period=10000, mode=Timer.PERIODIC, callback=lambda t: publish_telemetry(mqtt))

        await agent_task
        await command_task
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        if timer:
            timer.deinit()
        print("Exiting...")

if __name__ == "__main__":
    asyncio.run(main())
