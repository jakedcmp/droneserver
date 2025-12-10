"""
Tests for navigation tools:
- move_to_relative
- go_to_location
- get_home_position
- set_max_speed
- set_yaw
- reposition
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
import math

import sys
import os
from dataclasses import dataclass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))

from mavlinkmcp import (
    move_to_relative,
    go_to_location,
    get_home_position,
    set_max_speed,
    set_yaw,
    reposition,
)


@dataclass
class MockPosition:
    """Mock position telemetry"""
    latitude_deg: float = 33.6461
    longitude_deg: float = -117.8427
    absolute_altitude_m: float = 120.0
    relative_altitude_m: float = 10.0


class AsyncIteratorMock:
    """Helper to create async iterators for telemetry streams"""
    def __init__(self, items):
        self.items = items
        self.index = 0
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        if self.index < len(self.items):
            item = self.items[self.index]
            self.index += 1
            return item
        raise StopAsyncIteration


class TestMoveToRelative:
    """Tests for move_to_relative tool"""
    
    async def test_move_to_relative_success(self, mock_context, mock_drone):
        """Test successful relative movement"""
        result = await move_to_relative(mock_context, north_m=10.0, east_m=5.0, down_m=-2.0)
        
        assert result["status"] == "success"
        assert "target_position" in result
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_move_to_relative_calculates_target(self, mock_context, mock_drone):
        """Test that target position is calculated correctly"""
        result = await move_to_relative(mock_context, north_m=100.0, east_m=0.0, down_m=0.0)
        
        assert result["status"] == "success"
        # Moving 100m north should increase latitude
        assert result["target_position"]["latitude_deg"] > 33.6461
    
    async def test_move_to_relative_includes_altitude_change(self, mock_context, mock_drone):
        """Test altitude change (down is positive, so -5 = climb 5m)"""
        result = await move_to_relative(mock_context, north_m=0.0, east_m=0.0, down_m=-5.0)
        
        assert result["status"] == "success"
        # down=-5 means climbing 5m
        assert "5" in result["message"]
    
    async def test_move_to_relative_connection_timeout(self, disconnected_context):
        """Test movement fails when drone not connected"""
        result = await move_to_relative(disconnected_context, north_m=10.0, east_m=0.0, down_m=0.0)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_move_to_relative_exception(self, mock_context, mock_drone):
        """Test movement handles exceptions gracefully"""
        mock_drone.action.goto_location = AsyncMock(side_effect=Exception("Movement failed"))
        
        result = await move_to_relative(mock_context, north_m=10.0, east_m=0.0, down_m=0.0)
        
        assert result["status"] == "failed"


class TestGoToLocation:
    """Tests for go_to_location tool"""
    
    async def test_go_to_location_success(self, mock_context, mock_drone):
        """Test successful absolute navigation"""
        result = await go_to_location(
            mock_context,
            latitude_deg=33.65,
            longitude_deg=-117.84,
            absolute_altitude_m=50.0
        )
        
        assert result["status"] == "success"
        assert "target" in result
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_go_to_location_with_yaw(self, mock_context, mock_drone):
        """Test navigation with specified heading"""
        result = await go_to_location(
            mock_context,
            latitude_deg=33.65,
            longitude_deg=-117.84,
            absolute_altitude_m=50.0,
            yaw_deg=90.0
        )
        
        assert result["status"] == "success"
        assert result["target"]["yaw"] == 90.0
    
    async def test_go_to_location_invalid_latitude(self, mock_context, mock_drone):
        """Test rejection of invalid latitude"""
        result = await go_to_location(
            mock_context,
            latitude_deg=95.0,  # Invalid: > 90
            longitude_deg=-117.84,
            absolute_altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "latitude" in result["error"].lower()
    
    async def test_go_to_location_invalid_longitude(self, mock_context, mock_drone):
        """Test rejection of invalid longitude"""
        result = await go_to_location(
            mock_context,
            latitude_deg=33.65,
            longitude_deg=-200.0,  # Invalid: < -180
            absolute_altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "longitude" in result["error"].lower()
    
    async def test_go_to_location_connection_timeout(self, disconnected_context):
        """Test navigation fails when drone not connected"""
        result = await go_to_location(
            disconnected_context,
            latitude_deg=33.65,
            longitude_deg=-117.84,
            absolute_altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestGetHomePosition:
    """Tests for get_home_position tool"""
    
    async def test_get_home_position_success(self, mock_context, mock_drone):
        """Test successful home position read"""
        result = await get_home_position(mock_context)
        
        assert result["status"] == "success"
        assert "home" in result
        assert "latitude_deg" in result["home"]
        assert "longitude_deg" in result["home"]
        assert "absolute_altitude_m" in result["home"]
    
    async def test_get_home_position_values(self, mock_context, mock_drone):
        """Test home position returns correct values"""
        # Default: 33.6460, -117.8426
        result = await get_home_position(mock_context)
        
        assert result["home"]["latitude_deg"] == 33.6460
        assert result["home"]["longitude_deg"] == -117.8426
    
    async def test_get_home_position_connection_timeout(self, disconnected_context):
        """Test home position fails when drone not connected"""
        result = await get_home_position(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestSetMaxSpeed:
    """Tests for set_max_speed tool"""
    
    async def test_set_max_speed_success(self, mock_context, mock_drone):
        """Test successful speed limit set"""
        result = await set_max_speed(mock_context, speed_m_s=10.0)
        
        assert result["status"] == "success"
        assert "10" in result["message"]
        mock_drone.action.set_maximum_speed.assert_called_once_with(10.0)
    
    async def test_set_max_speed_includes_kmh(self, mock_context, mock_drone):
        """Test speed includes km/h conversion"""
        result = await set_max_speed(mock_context, speed_m_s=10.0)
        
        assert result["status"] == "success"
        assert "speed_kmh" in result
        assert result["speed_kmh"] == 36.0  # 10 * 3.6
    
    async def test_set_max_speed_rejects_negative(self, mock_context, mock_drone):
        """Test rejection of negative speed"""
        result = await set_max_speed(mock_context, speed_m_s=-5.0)
        
        assert result["status"] == "failed"
        assert "invalid" in result["error"].lower()
    
    async def test_set_max_speed_rejects_too_high(self, mock_context, mock_drone):
        """Test rejection of speed > 30 m/s"""
        result = await set_max_speed(mock_context, speed_m_s=50.0)
        
        assert result["status"] == "failed"
        assert "too high" in result["error"].lower() or "30" in result["error"]
    
    async def test_set_max_speed_connection_timeout(self, disconnected_context):
        """Test speed set fails when drone not connected"""
        result = await set_max_speed(disconnected_context, speed_m_s=10.0)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestSetYaw:
    """Tests for set_yaw tool"""
    
    async def test_set_yaw_success(self, mock_context, mock_drone):
        """Test successful yaw set"""
        result = await set_yaw(mock_context, yaw_deg=90.0)
        
        assert result["status"] == "success"
        assert result["yaw_degrees"] == 90.0
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_set_yaw_cardinal_directions(self, mock_context, mock_drone):
        """Test cardinal direction calculation"""
        # Test North - need to reset the position iterator for each call
        # Use local AsyncIteratorMock and MockPosition defined above
        mock_drone.telemetry.position = MagicMock(
            return_value=AsyncIteratorMock([MockPosition()])
        )
        result = await set_yaw(mock_context, yaw_deg=0.0)
        assert result["cardinal_direction"] == "N"
        
        # Reset mock and position iterator for East test
        mock_drone.action.goto_location.reset_mock()
        mock_drone.telemetry.position = MagicMock(
            return_value=AsyncIteratorMock([MockPosition()])
        )
        
        # Test East
        result = await set_yaw(mock_context, yaw_deg=90.0)
        assert result["cardinal_direction"] == "E"
    
    async def test_set_yaw_normalizes_angle(self, mock_context, mock_drone):
        """Test yaw angle normalization to 0-360"""
        result = await set_yaw(mock_context, yaw_deg=450.0)
        
        assert result["status"] == "success"
        assert result["yaw_degrees"] == 90.0  # 450 % 360 = 90
    
    async def test_set_yaw_rejects_negative_rate(self, mock_context, mock_drone):
        """Test rejection of negative yaw rate"""
        result = await set_yaw(mock_context, yaw_deg=90.0, yaw_rate_deg_s=-10.0)
        
        assert result["status"] == "failed"
        assert "yaw rate" in result["error"].lower()
    
    async def test_set_yaw_connection_timeout(self, disconnected_context):
        """Test yaw set fails when drone not connected"""
        result = await set_yaw(disconnected_context, yaw_deg=90.0)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestReposition:
    """Tests for reposition tool"""
    
    async def test_reposition_success(self, mock_context, mock_drone):
        """Test successful reposition"""
        result = await reposition(
            mock_context,
            latitude_deg=33.65,
            longitude_deg=-117.84,
            altitude_m=50.0
        )
        
        assert result["status"] == "success"
        assert "target" in result
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_reposition_invalid_latitude(self, mock_context, mock_drone):
        """Test rejection of invalid latitude"""
        result = await reposition(
            mock_context,
            latitude_deg=100.0,  # Invalid
            longitude_deg=-117.84,
            altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "latitude" in result["error"].lower()
    
    async def test_reposition_invalid_longitude(self, mock_context, mock_drone):
        """Test rejection of invalid longitude"""
        result = await reposition(
            mock_context,
            latitude_deg=33.65,
            longitude_deg=-190.0,  # Invalid
            altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "longitude" in result["error"].lower()
    
    async def test_reposition_connection_timeout(self, disconnected_context):
        """Test reposition fails when drone not connected"""
        result = await reposition(
            disconnected_context,
            latitude_deg=33.65,
            longitude_deg=-117.84,
            altitude_m=50.0
        )
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()

