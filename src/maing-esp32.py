# from time import sleep

import json
import network
import machine
import os
import sys
import time
import ubinascii
import asyncio
from machine import Timer
from asyncio import sleep
import requests

import config

#
# External dependencies (installing/fetching if necessary)
#
try:
    # from umqtt.simple import MQTTClient
    from umqtt.robust import MQTTClient
except ImportError:
    print("Installing missing dependencies")
    import mip

    mip.install("umqtt.simple")
    mip.install("umqtt.robust")
    from umqtt.robust import MQTTClient

try:
    from primitives import Queue
except ImportError:
    print("Installing missing dependencies")
    import mip

    mip.install("github:peterhinch/micropython-async/v3/primitives")
    from primitives import Queue

# Application info
__APPLICATION_NAME__ = "micropython-agent"
__APPLICATION_VERSION__ = "1.0.1"

# Device info
serial_no = ubinascii.hexlify(machine.unique_id()).decode()
device_id = f"esp32-{serial_no}"
topic_identifier = f"te/device/{device_id}//"

# Device in/ut
adcpin = 36
sensor = machine.ADC(adcpin)
led = machine.Pin(2, machine.Pin.OUT)


def read_temperature():
    adc_value = sensor.read_u16()
    volt = (3.3 / 65535) * adc_value
    temperature = 27 - (volt - 0.706) / 0.001721
    return round(temperature, 1)


async def blink_led_async(times=6, rate=0.1):
    for i in range(0, times):
        led(not led())

        # led.toggle()
        await sleep(rate)
    led.on()


def blink_led(times=6, rate=0.1):
    for i in range(0, times):
        # led.toggle()
        led(not led())
        time.sleep(rate)
    led.on()


async def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    while wlan.isconnected() == False:
        print("Waiting for connection..")
        await sleep(1)
    ip = wlan.ifconfig()[0]
    print(f"Connected on {ip}")
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


class Restart(Machine):
    def init(self, context: Context):
        return self.executing

    def executing(self, context: Context):
        context.restart_requested = True
        return self.restarting

    def restarting(self, context: Context):
        return self.successful


def download_file(url, out_file):
    print(f"Downloading file. url={url}")
    response = requests.get(url)
    response_status_code = response.status_code
    response_text = response.text
    response.close()
    if response_status_code != 200:
        raise ValueError(f"Failed to download url. url={url}, status_code={response_status_code}")

    with open(out_file, "w") as file:
        file.write(response_text)


class SoftwareUpdate(Machine):
    def init(self, context: Context):
        return self.executing

    # {"status":"init","updateList":[{"type":"default","modules":[{"name":"micropython-agent","version":"0.0.1","url":"http://127.0.0.1:8001/c8y/inventory/binaries/4548385","action":"install"}]}]}

    def executing(self, context: Context):
        print(f"Installing software: {context.message}")
        for group in context.message.get("updateList", []):
            package_type = group.get("type") or "default"

            if package_type not in ["default", "micropython"]:
                context.reason = f"Software type is not supported. type={package_type}"
                return self.failed

            for module in group["modules"]:
                if module["url"]:
                    out_file = "main.py.tmp"
                    try:
                        download_file(module["url"], out_file)
                    except Exception as ex:
                        context.reason = str(ex)
                        return self.failed

                    print("Replacing main")
                    APPLICATION_FILE = "main.py"
                    os.rename(out_file, APPLICATION_FILE)
                    context.restart_requested = True
                    return self.restarting

        return self.successful

    def restarting(self, context: Context):
        return self.successful

    def successful(self, context: Context):
        return None


class SoftwareList(Machine):
    def executing(self, context: Context):
        context.message["currentSoftwareList"] = [
            {
                "type": "",
                "modules": [
                    {"name": __APPLICATION_NAME__, "version": __APPLICATION_VERSION__},
                ],
            }
        ]
        return self.successful


class Unsupported(Machine):
    def init(self, context: Context):
        return self.failed


async def run(outgoing_queue, context: Context, state_machine: Machine):
    try:
        print(
            f"Starting state machine: {type(state_machine).__name__}, context={context.message}"
        )
        state = getattr(state_machine, context.message.get("status", "init"), None)
        error_count = 0
        while state is not None:
            try:
                print(f"Running state: {state.__name__}")
                state = state(context)
                if state is not None:
                    context.message["status"] = state.__name__
                    print(f"Save next state: {state.__name__}")
                    if context.reason:
                        context.message["reason"] = context.reason

                    await outgoing_queue.put(
                        (context.topic, json.dumps(context.message), True, 1)
                    )
                    print("Queued outgoing message")
                    # await sleep(1)

                    if context.restart_requested:
                        print("Restart requested. Stopping state machine execution")
                        await outgoing_queue.put(
                            (
                                f"{topic_identifier}/e/reboot",
                                json.dumps(
                                    {
                                        "text": "Restarting device",
                                    }
                                ),
                                False,
                                0,
                            )
                        )
                        return True
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


async def command_executor(queue, client, outgoing_queue):
    count = 1
    print("Starting command executor")
    while True:
        topic, command_type, command = await queue.get()  # Blocks until data is ready
        print(
            f"[id={count}, type={command_type}] Processing command. {topic} {command}"
        )

        context = Context(topic, command_type, command)
        state_machine = get_machine(context)
        if state_machine:
            should_restart = await run(outgoing_queue, context, state_machine)
            if should_restart:
                print("Restart requested")
                await sleep(5)
                print("RESTARTING DEVICE NOW")
                machine.reset()

        print(f"[id={count}, type={command_type}] Finished command")
        count += 1


#
# Telemetry
#
def publish_telemetry(outgoing_queue):
    message = {
        "temp": read_temperature(),
    }
    print(f"Queuing telemetry data. {message}")
    blink_led(2, 0.15)
    try:
        outgoing_queue.put_nowait(
            (
                f"{topic_identifier}/m/environment",
                json.dumps(message),
                False,
                0,
            )
        )
    except Exception as ex:
        pass


#
# Agent
#
async def agent(queue, client, outgoing_queue):
    try:

        def _queue_message(topic, message):
            if len(message):
                try:
                    command = json.loads(message.decode("utf-8"))
                    if not isinstance(command, dict):
                        raise ValueError("payload is not a dictionary")

                    # TODO: Keep track of which operations are already known to be in progress
                    # or if it is resuming after a restart.
                    command_status = command.get("status", "")
                    # if command_status in ["successful", "failed"]:
                    if command_status not in ["init", "restarting"]:
                        raise ValueError(
                            f"command is already in final state. {command_status}"
                        )

                    command_type = topic.decode("utf-8").split("/")[-2]
                    print(f"Add message to queue: {topic} {message}")
                    queue.put_nowait((topic, command_type, command))
                except Exception as ex:
                    pass
                    # print(f"Ignoring message. topic={topic}, message={message}, error={ex}")

        # register device
        client.publish(
            f"{topic_identifier}",
            json.dumps(
                {
                    "@type": "child-device",
                    "name": device_id,
                    "type": "micropython",
                }
            ),
            qos=1,
            retain=True,
        )

        # publish hardware info
        client.publish(
            f"{topic_identifier}/twin/c8y_Hardware",
            json.dumps(
                {
                    "serialNumber": serial_no,
                    "model": "ESP32-WROOM-32",
                    "revision": "RP2040",
                }
            ),
            qos=1,
            retain=True,
        )

        # register support for commands
        client.publish(f"{topic_identifier}/cmd/restart", b"{}", retain=True, qos=1)
        client.publish(
            f"{topic_identifier}/cmd/software_update", b"{}", retain=True, qos=1
        )
        client.publish(
            f"{topic_identifier}/cmd/software_list", b"{}", retain=True, qos=1
        )

        # startup messages
        client.publish(
            f"{topic_identifier}/e/boot",
            json.dumps(
                {"text": f"Application started. version={__APPLICATION_VERSION__}"}
            ),
            qos=1,
        )

        # subscribe to commands
        client.subscribe(f"{topic_identifier}/cmd/+/+")
        print("Subscribed to commands topic")

        # give visual queue that the device booted up
        await blink_led_async()

        while True:
            try:
                # print("Waiting for msg")
                # client.wait_msg()   # blocking
                client.check_msg()  # non-blocking
                await asyncio.sleep(1)
            except Exception as ex:
                print(f"Unexpected error: {ex}")
    except Exception as ex:
        print(f"Failed to connect, retrying in 5 seconds. error={ex}")
        raise


async def publisher(queue, client):
    print("Starting publisher")
    while True:
        try:
            topic, payload, retain, qos = await queue.get()  # Blocks until data is ready
            print(
                f"Publishing message: topic={topic}, payload={payload}, retain={retain}, qos={qos}"
            )
            client.publish(topic, payload, retain=retain, qos=qos)
        except Exception as ex:
            print(f"Failed to publish message. ex={ex}")


#
# Main
#
async def main():
    try:
        timer = Timer(1)
        queue = Queue(30)
        outgoing_queue = Queue(30)
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

        def _queue_message(topic, message):
            if len(message):
                try:
                    command = json.loads(message.decode("utf-8"))
                    if not isinstance(command, dict):
                        raise ValueError("payload is not a dictionary")

                    # TODO: Keep track of which operations are already known to be in progress
                    # or if it is resuming after a restart.
                    command_status = command.get("status", "")
                    # if command_status in ["successful", "failed"]:
                    if command_status not in ["init", "restarting"]:
                        raise ValueError(
                            f"command is already in final state. {command_status}"
                        )

                    command_type = topic.decode("utf-8").split("/")[-2]
                    print(f"Add message to queue: {topic} {message}")
                    queue.put_nowait((topic, command_type, command))
                except Exception as ex:
                    pass
                    # print(f"Ignoring message. topic={topic}, message={message}, error={ex}")

        # Block until the agent is connected for the first time?
        # Note: does this make sense?
        mqtt.set_callback(_queue_message)
        connected = False
        while not connected:
            try:
                print(
                    f"Connecting to thin-edge.io broker: broker={mqtt.server}:{mqtt.port}, client_id={mqtt.client_id}"
                )
                mqtt.connect()
                connected = True
                await sleep(1)
                mqtt.check_msg()
                # mqtt.wait_msg()   # blocking
                print("Connected to thin-edge.io broker")
            except Exception as ex:
                print("Connection failed retrying later")
                await sleep(5)

        print("Starting background tasks")
        agent_task = asyncio.create_task(agent(queue, mqtt, outgoing_queue))
        command_task = asyncio.create_task(
            command_executor(queue, mqtt, outgoing_queue)
        )
        publisher_task = asyncio.create_task(publisher(outgoing_queue, mqtt))

        await asyncio.sleep(5)

        # NOTE: use Timer over async tasks due to a blocking issue when publishing MQTT messages
        # This could be solved in the future if necessary
        timer.init(
            period=10000,
            mode=Timer.PERIODIC,
            callback=lambda t: publish_telemetry(outgoing_queue)
        )

        await agent_task
        await command_task
        await publisher_task
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        try:
            # if timer:
            timer.deinit()
        except:
            pass
        print("Exiting...")


if __name__ == "__main__":
    asyncio.run(main())

