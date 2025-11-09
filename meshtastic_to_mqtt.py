"\"\"\"Meshtastic to MQTT gateway for Wiren Board.

Connects to a Meshtastic device via serial, listens for telemetry/user data,
and republishes the parsed values to a local MQTT broker using Wiren Board
control topics. Includes simple alerting for low voltage, poor SNR, and node
inactivity.
\"\"\"

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
from meshtastic.serial_interface import SerialInterface
from pubsub import pub


LOGGER = logging.getLogger("meshtastic_mqtt")
DEFAULT_SERIAL_PATH = "/dev/ttyACM0"
DEFAULT_MQTT_HOST = "localhost"
DEFAULT_MQTT_PORT = 1883
MQTT_TOPIC_TEMPLATE = "/devices/meshtastic/controls/{}/on"


@dataclass
class AlertState:
    low_voltage: bool = False
    poor_snr: bool = False
    inactive: bool = False
    last_reported: Dict[str, datetime] = field(default_factory=dict)


class MeshtasticMQTTGateway:
    """Bridge Meshtastic pubsub events to MQTT topics."""

    def __init__(
        self,
        serial_path: str = DEFAULT_SERIAL_PATH,
        mqtt_host: str = DEFAULT_MQTT_HOST,
        mqtt_port: int = DEFAULT_MQTT_PORT,
        inactivity_minutes: int = 30,
    ) -> None:
        self.serial_path = serial_path
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.inactivity_threshold = timedelta(minutes=inactivity_minutes)

        self._last_seen: Dict[str, datetime] = {}
        self._alert_state: Dict[str, AlertState] = {}
        self._stop_event = threading.Event()

        self.mqtt_client = mqtt.Client(client_id="meshtastic-mqtt-gateway")
        self.mqtt_client.enable_logger(LOGGER)
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        self.mqtt_client.loop_start()

        LOGGER.info("Connected to MQTT broker at %s:%s", self.mqtt_host, self.mqtt_port)

        self.interface = SerialInterface(devPath=self.serial_path)
        LOGGER.info("Connected to Meshtastic device on %s", self.serial_path)

        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_user, "meshtastic.receive.user")
        pub.subscribe(self._on_data, "meshtastic.receive.data")
        pub.subscribe(self._on_position, "meshtastic.receive.position")

        self._inactivity_thread = threading.Thread(
            target=self._monitor_inactivity, name="inactivity-watchdog", daemon=True
        )
        self._inactivity_thread.start()

    def _on_mqtt_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
        if rc == 0:
            LOGGER.info("MQTT connected successfully")
        else:
            LOGGER.warning("MQTT connection failed with code: %s", rc)

    def _on_mqtt_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        LOGGER.warning("MQTT disconnected with code: %s", rc)

    def _publish_value(self, node_id: str, parameter: str, value: Any) -> None:
        topic = MQTT_TOPIC_TEMPLATE.format(f"{node_id}_{parameter}")
        payload = value if isinstance(value, (str, bytes)) else json.dumps(value)
        LOGGER.debug("Publishing %s => %s", topic, payload)
        self.mqtt_client.publish(topic, payload, retain=True)

    def _record_seen(self, node_id: str) -> None:
        now = datetime.utcnow()
        self._last_seen[node_id] = now
        self._alert_state.setdefault(node_id, AlertState())

    def _set_alert(self, node_id: str, alert_name: str, triggered: bool, detail: str) -> None:
        alert_state = self._alert_state.setdefault(node_id, AlertState())
        current = getattr(alert_state, alert_name)
        if current == triggered:
            return

        setattr(alert_state, alert_name, triggered)
        status_text = "ALERT" if triggered else "OK"
        message = f"{status_text}: {detail}"
        parameter = f"{alert_name}_alert"
        self._publish_value(node_id, parameter, message)
        LOGGER.info("Alert state change for %s (%s): %s", node_id, alert_name, message)
        alert_state.last_reported[alert_name] = datetime.utcnow()

    def _publish_packet_metrics(self, node_id: str, packet: Dict[str, Any]) -> None:
        rssi = packet.get("rxRssi")
        snr = packet.get("rxSnr")
        if rssi is not None:
            self._publish_value(node_id, "rssi_dbm", rssi)
        if snr is not None:
            self._publish_value(node_id, "snr_db", snr)
            self._set_alert(node_id, "poor_snr", snr < 2.0, f"SNR {snr} dB")

        decoded = packet.get("decoded", {})
        telemetry = decoded.get("telemetry") or decoded.get("deviceMetrics")
        if isinstance(telemetry, dict):
            voltage = telemetry.get("voltage") or telemetry.get("batteryVoltage")
            battery = telemetry.get("batteryLevel") or telemetry.get("battery_percent")
            if voltage is not None:
                self._publish_value(node_id, "voltage_v", voltage)
                self._set_alert(node_id, "low_voltage", voltage < 3.5, f"Voltage {voltage} V")
            if battery is not None:
                self._publish_value(node_id, "battery_pct", battery)

        position = decoded.get("position")
        if isinstance(position, dict):
            lat = position.get("latitude")
            lon = position.get("longitude")
            alt = position.get("altitude")
            if lat is not None and lon is not None:
                self._publish_value(node_id, "latitude", lat)
                self._publish_value(node_id, "longitude", lon)
            if alt is not None:
                self._publish_value(node_id, "altitude_m", alt)

    def _sanitize_node_id(self, node_id: Optional[str]) -> str:
        if not node_id:
            return "unknown"
        return node_id.strip().replace(" ", "_").replace("/", "_")

    def _extract_node_id(self, packet: Dict[str, Any]) -> str:
        raw_id = packet.get("fromId") or packet.get("nodeId") or packet.get("user", {}).get("id")
        return self._sanitize_node_id(raw_id)

    # --- Meshtastic event handlers -------------------------------------------------

    def _on_receive(self, packet: Dict[str, Any]) -> None:
        node_id = self._extract_node_id(packet)
        LOGGER.debug("Received packet from %s", node_id)
        self._record_seen(node_id)
        self._publish_packet_metrics(node_id, packet)

    def _on_user(self, user_info: Dict[str, Any]) -> None:
        node_id = self._extract_node_id(user_info)
        user = user_info.get("user", {})
        LOGGER.debug("User info update for %s: %s", node_id, user)
        self._record_seen(node_id)
        if user:
            if "longName" in user:
                self._publish_value(node_id, "long_name", user["longName"])
            if "shortName" in user:
                self._publish_value(node_id, "short_name", user["shortName"])

    def _on_data(self, packet: Dict[str, Any]) -> None:
        node_id = self._extract_node_id(packet)
        LOGGER.debug("Data message from %s", node_id)
        self._record_seen(node_id)
        decoded = packet.get("decoded", {})
        payload = decoded.get("payload")
        if payload is not None:
            self._publish_value(node_id, "payload_raw", payload)

    def _on_position(self, packet: Dict[str, Any]) -> None:
        node_id = self._extract_node_id(packet)
        LOGGER.debug("Position update from %s", node_id)
        self._record_seen(node_id)
        decoded = packet.get("decoded", {})
        position = decoded.get("position") or packet.get("position")
        if isinstance(position, dict):
            lat = position.get("latitude")
            lon = position.get("longitude")
            alt = position.get("altitude")
            if lat is not None and lon is not None:
                self._publish_value(node_id, "latitude", lat)
                self._publish_value(node_id, "longitude", lon)
            if alt is not None:
                self._publish_value(node_id, "altitude_m", alt)

    # --- Inactivity monitoring -----------------------------------------------------

    def _monitor_inactivity(self) -> None:
        LOGGER.info("Starting inactivity monitor (threshold %s)", self.inactivity_threshold)
        while not self._stop_event.wait(60):
            now = datetime.utcnow()
            for node_id, last_seen in list(self._last_seen.items()):
                inactive = now - last_seen > self.inactivity_threshold
                self._set_alert(
                    node_id,
                    "inactive",
                    inactive,
                    f"Last seen {int((now - last_seen).total_seconds() // 60)} minutes ago",
                )

    # --- Lifecycle management ------------------------------------------------------

    def stop(self) -> None:
        LOGGER.info("Stopping gateway")
        self._stop_event.set()
        if self._inactivity_thread.is_alive():
            self._inactivity_thread.join(timeout=2)
        try:
            self.interface.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Error closing Meshtastic interface: %s", exc)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def run(self) -> None:
        LOGGER.info("Gateway running; press Ctrl+C to exit")
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            LOGGER.info("Keyboard interrupt received")
        finally:
            self.stop()


def configure_logging(verbosity: int = 0) -> None:
    level = logging.INFO if verbosity == 0 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    verbosity = 1 if "-v" in sys.argv or "--verbose" in sys.argv else 0
    configure_logging(verbosity)

    gateway = MeshtasticMQTTGateway()

    def _handle_signal(signum: int, frame: Optional[Any]) -> None:  # noqa: ANN401
        LOGGER.info("Signal %s received, shutting down", signum)
        gateway.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    gateway.run()


if __name__ == "__main__":
    main()

