# meshtastic-mqtt-gateway
Python gateway that bridges Meshtastic telemetry into Wiren Board MQTT controls.

## Features
- Connects to a Meshtastic device via `/dev/ttyACM0` using the official Python API.
- Subscribes to telemetry, user, data, and position events exposed through `pypubsub`.
- Publishes node data to Wiren Board topics such as `/devices/meshtastic/controls/<node_id>_<parameter>/on`.
- Emits basic alerts for low voltage, poor SNR, and node inactivity (> 30 minutes).
- Ships with a `systemd` service unit for automatic startup on Wiren Board.

## Prerequisites
- Wiren Board with Python 3.9+ installed.
- Local MQTT broker (Wiren Board includes Mosquitto by default).
- Meshtastic device connected via USB and exposed as `/dev/ttyACM0` (adjust if different).

## Installation
1. Copy the project files onto the Wiren Board (e.g. to `/opt/meshtastic-mqtt-gateway`):
   ```bash
   sudo mkdir -p /opt/meshtastic-mqtt-gateway
   sudo cp meshtastic_to_mqtt.py /opt/meshtastic-mqtt-gateway/
   sudo cp meshtastic-mqtt.service /etc/systemd/system/
   ```
2. Install Python dependencies:
   ```bash
   sudo apt update
   sudo apt install -y python3-pip
   sudo pip3 install --upgrade meshtastic pypubsub paho-mqtt
   ```
3. (Optional) Adjust `meshtastic-mqtt.service`:
   - Update `User`/`Group` if you run services under a different account.
   - Change `WorkingDirectory`, `ExecStart`, or serial device path if required.

## Usage
- Run manually for testing:
  ```bash
  cd /opt/meshtastic-mqtt-gateway
  python3 meshtastic_to_mqtt.py --verbose
  ```
- Enable the service for automatic start:
  ```bash
  sudo systemctl daemon-reload
  sudo systemctl enable meshtastic-mqtt.service
  sudo systemctl start meshtastic-mqtt.service
  sudo systemctl status meshtastic-mqtt.service
  ```

## MQTT Topics
- Metrics are published as `/devices/meshtastic/controls/<node_id>_<parameter>/on`.
- Parameters include `voltage_v`, `battery_pct`, `rssi_dbm`, `snr_db`, `latitude`, `longitude`, `altitude_m`, and raw payloads.
- Alerts publish on parameters `low_voltage_alert`, `poor_snr_alert`, and `inactive_alert` with human-readable messages.

## Alerts
- **Low voltage**: Triggered when telemetry voltage < 3.5 V.
- **Poor SNR**: Triggered when received SNR < 2 dB.
- **Inactivity**: Triggered when a node is silent for more than 30 minutes.
Alerts reset to `OK` once metrics return to normal ranges.

## Development Notes
- Logging defaults to INFO; add `--verbose` for debug output.
- The script retains MQTT messages (`retain=True`) so Wiren Board controls display the latest values.
- Modify `DEFAULT_SERIAL_PATH` or MQTT connection details in `meshtastic_to_mqtt.py` as needed for your environment.
