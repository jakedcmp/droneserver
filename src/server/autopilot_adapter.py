from __future__ import annotations

import math

from mavsdk import System

SUPPORTED_AUTOPILOT_BACKENDS = ("px4", "ardupilot")
DEFAULT_AUTOPILOT_BACKEND = "px4"


class AutopilotAdapter:
    backend_name: str
    capabilities: dict[str, bool]

    async def get_backend_info(self) -> dict:
        ...

    async def set_flight_mode(self, mode: str) -> dict:
        ...

    async def go_to_location(
        self,
        latitude_deg: float,
        longitude_deg: float,
        absolute_altitude_m: float,
        yaw_deg: float,
    ) -> dict:
        ...

    async def move_to_relative(
        self,
        north_m: float,
        east_m: float,
        down_m: float,
        yaw_deg: float,
    ) -> dict:
        ...

    async def hold_position(self) -> dict:
        ...


def resolve_autopilot_backend(raw_backend: str | None) -> str:
    backend = (raw_backend or DEFAULT_AUTOPILOT_BACKEND).strip().lower()
    aliases = {
        "px4": "px4",
        "ardupilot": "ardupilot",
        "apm": "ardupilot",
        "ardu": "ardupilot",
    }
    resolved = aliases.get(backend)
    if resolved is None:
        supported = ", ".join(SUPPORTED_AUTOPILOT_BACKENDS)
        raise ValueError(
            f"Unsupported AUTOPILOT_BACKEND '{raw_backend}'. Supported values: {supported}"
        )
    return resolved


def create_autopilot_adapter(drone: System, backend_name: str) -> AutopilotAdapter:
    if backend_name == "px4":
        return PX4Adapter(drone)
    if backend_name == "ardupilot":
        return ArduPilotAdapter(drone)
    raise ValueError(f"Unsupported backend '{backend_name}'")


class BaseAutopilotAdapter(AutopilotAdapter):
    backend_name = "unknown"
    capabilities: dict[str, bool] = {}

    def __init__(self, drone: System):
        self.drone = drone

    async def get_backend_info(self) -> dict:
        return {
            "name": self.backend_name,
            "capabilities": self.capabilities,
        }

    async def go_to_location(
        self,
        latitude_deg: float,
        longitude_deg: float,
        absolute_altitude_m: float,
        yaw_deg: float,
    ) -> dict:
        await self.drone.action.goto_location(
            latitude_deg,
            longitude_deg,
            absolute_altitude_m,
            yaw_deg,
        )
        return {
            "semantic_mode": "GUIDED",
            "adapter_action": "goto_location",
            "target_position": {
                "latitude_deg": latitude_deg,
                "longitude_deg": longitude_deg,
                "altitude_m": absolute_altitude_m,
                "yaw_deg": yaw_deg,
            },
        }

    async def move_to_relative(
        self,
        north_m: float,
        east_m: float,
        down_m: float,
        yaw_deg: float,
    ) -> dict:
        position = await self._read_position()
        current_lat = position.latitude_deg
        current_lon = position.longitude_deg
        current_alt = position.absolute_altitude_m

        lat_offset_deg = north_m / 111320.0
        cos_lat = math.cos(math.radians(current_lat))
        if abs(cos_lat) < 1e-6:
            raise ValueError("Relative movement is undefined at extreme latitudes.")
        lon_offset_deg = east_m / (111320.0 * cos_lat)
        target_alt = current_alt - down_m
        target_lat = current_lat + lat_offset_deg
        target_lon = current_lon + lon_offset_deg

        goto_result = await self.go_to_location(target_lat, target_lon, target_alt, yaw_deg)
        goto_result["current_position"] = {
            "latitude_deg": current_lat,
            "longitude_deg": current_lon,
            "absolute_altitude_m": current_alt,
            "relative_altitude_m": position.relative_altitude_m,
        }
        return goto_result

    async def _read_position(self):
        return await self.drone.telemetry.position().__anext__()


class PX4Adapter(BaseAutopilotAdapter):
    backend_name = "px4"
    capabilities = {
        "native_guided_mode": False,
        "semantic_guided_intent": True,
        "hold_action": True,
        "goto_location": True,
    }

    async def set_flight_mode(self, mode: str) -> dict:
        if mode in ("HOLD", "LOITER"):
            await self.drone.action.hold()
            return {
                "semantic_mode": "HOLD",
                "adapter_action": "hold",
                "message": "Flight mode changed to HOLD",
            }
        if mode in ("RTL", "RETURN_TO_LAUNCH"):
            await self.drone.action.return_to_launch()
            return {
                "semantic_mode": "RTL",
                "adapter_action": "return_to_launch",
                "message": "Flight mode changed to RTL",
            }
        if mode == "LAND":
            await self.drone.action.land()
            return {
                "semantic_mode": "LAND",
                "adapter_action": "land",
                "message": "Flight mode changed to LAND",
            }
        if mode == "GUIDED":
            return {
                "semantic_mode": "GUIDED",
                "adapter_action": "none",
                "message": "PX4 accepted GUIDED as a navigation intent",
                "note": "PX4 does not expose a native GUIDED mode via MAVSDK. Use go_to_location() or move_to_relative() to command motion.",
            }
        raise ValueError(f"Unsupported mode '{mode}'")

    async def hold_position(self) -> dict:
        position = await self._read_position()
        await self.drone.action.hold()
        return {
            "semantic_mode": "HOLD",
            "adapter_action": "hold",
            "message": "Drone holding position",
            "position": {
                "latitude_deg": position.latitude_deg,
                "longitude_deg": position.longitude_deg,
                "altitude_m": position.relative_altitude_m,
                "altitude_rel": position.relative_altitude_m,
            },
            "note": "PX4 uses HOLD for station-keeping.",
        }

    async def go_to_location(
        self,
        latitude_deg: float,
        longitude_deg: float,
        absolute_altitude_m: float,
        yaw_deg: float,
    ) -> dict:
        result = await super().go_to_location(
            latitude_deg,
            longitude_deg,
            absolute_altitude_m,
            yaw_deg,
        )
        result["note"] = "PX4 movement uses direct goto commands without a separate GUIDED mode switch."
        return result


class ArduPilotAdapter(BaseAutopilotAdapter):
    backend_name = "ardupilot"
    capabilities = {
        "native_guided_mode": True,
        "semantic_guided_intent": True,
        "hold_action": False,
        "goto_location": True,
    }

    async def set_flight_mode(self, mode: str) -> dict:
        if mode in ("HOLD", "LOITER"):
            position = await self._read_position()
            await self.drone.action.goto_location(
                position.latitude_deg,
                position.longitude_deg,
                position.absolute_altitude_m,
                float("nan"),
            )
            return {
                "semantic_mode": "HOLD",
                "adapter_action": "goto_current_position",
                "message": "Flight mode changed to HOLD/LOITER",
                "note": "ArduPilot hold semantics use a current-position goto to preserve station keeping.",
            }
        if mode in ("RTL", "RETURN_TO_LAUNCH"):
            await self.drone.action.return_to_launch()
            return {
                "semantic_mode": "RTL",
                "adapter_action": "return_to_launch",
                "message": "Flight mode changed to RTL",
            }
        if mode == "LAND":
            await self.drone.action.land()
            return {
                "semantic_mode": "LAND",
                "adapter_action": "land",
                "message": "Flight mode changed to LAND",
            }
        if mode == "GUIDED":
            position = await self._read_position()
            await self.drone.action.goto_location(
                position.latitude_deg,
                position.longitude_deg,
                position.absolute_altitude_m,
                float("nan"),
            )
            return {
                "semantic_mode": "GUIDED",
                "adapter_action": "goto_current_position",
                "message": "Flight mode changed to GUIDED",
                "note": "ArduPilot enters GUIDED semantics when it receives a goto command.",
            }
        raise ValueError(f"Unsupported mode '{mode}'")

    async def hold_position(self) -> dict:
        position = await self._read_position()
        await self.drone.action.goto_location(
            position.latitude_deg,
            position.longitude_deg,
            position.absolute_altitude_m,
            float("nan"),
        )
        return {
            "semantic_mode": "GUIDED",
            "adapter_action": "goto_current_position",
            "message": "Drone holding position in GUIDED mode",
            "position": {
                "latitude_deg": position.latitude_deg,
                "longitude_deg": position.longitude_deg,
                "altitude_m": position.relative_altitude_m,
                "altitude_rel": position.relative_altitude_m,
            },
            "note": "Using GUIDED station-keeping instead of LOITER to avoid altitude drift.",
        }
