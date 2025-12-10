"""
Tests for emergency & safety tools:
- return_to_launch
- kill_motors
- get_battery
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import sys
import os
from dataclasses import dataclass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))

from mavlinkmcp import (
    return_to_launch,
    kill_motors,
    get_battery,
)


@dataclass
class MockBattery:
    """Mock battery telemetry"""
    voltage_v: float = 16.2
    remaining_percent: float = 0.85


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


def make_async_iter(item):
    """Create an async iterator that yields a single item"""
    return AsyncIteratorMock([item])


class TestReturnToLaunch:
    """Tests for return_to_launch (RTL) tool"""
    
    async def test_rtl_success(self, mock_context, mock_drone):
        """Test successful return to launch"""
        result = await return_to_launch(mock_context)
        
        assert result["status"] == "success"
        assert "return" in result["message"].lower() or "home" in result["message"].lower()
        mock_drone.action.return_to_launch.assert_called_once()
    
    async def test_rtl_connection_timeout(self, disconnected_context):
        """Test RTL fails when drone not connected"""
        result = await return_to_launch(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_rtl_exception(self, mock_context, mock_drone):
        """Test RTL handles exceptions gracefully"""
        mock_drone.action.return_to_launch = AsyncMock(side_effect=Exception("RTL failed"))
        
        result = await return_to_launch(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()


class TestKillMotors:
    """Tests for kill_motors (emergency) tool"""
    
    async def test_kill_motors_success(self, mock_context, mock_drone):
        """Test successful emergency motor kill"""
        result = await kill_motors(mock_context)
        
        assert result["status"] == "success"
        assert "kill" in result["message"].lower() or "motor" in result["message"].lower()
        assert "warning" in result  # Should include warning about drone falling
        mock_drone.action.kill.assert_called_once()
    
    async def test_kill_motors_connection_timeout(self, disconnected_context):
        """Test kill fails when drone not connected"""
        result = await kill_motors(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_kill_motors_exception(self, mock_context, mock_drone):
        """Test kill handles exceptions gracefully"""
        mock_drone.action.kill = AsyncMock(side_effect=Exception("Kill failed"))
        
        result = await kill_motors(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()


class TestGetBattery:
    """Tests for get_battery tool"""
    
    async def test_get_battery_success(self, mock_context, mock_drone):
        """Test successful battery read"""
        result = await get_battery(mock_context)
        
        assert result["status"] == "success"
        assert "battery" in result
        assert "voltage_v" in result["battery"]
        assert "remaining_percent" in result["battery"]
    
    async def test_get_battery_values(self, mock_context, mock_drone):
        """Test battery returns correct values"""
        # Default mock: 16.2V, 85%
        result = await get_battery(mock_context)
        
        assert result["battery"]["voltage_v"] == 16.2
        assert result["battery"]["remaining_percent"] == 85.0  # 0.85 * 100
    
    async def test_get_battery_low_warning(self, mock_context, mock_drone):
        """Test low battery warning at <20%"""
        mock_drone.telemetry.battery = MagicMock(
            return_value=make_async_iter(MockBattery(voltage_v=14.0, remaining_percent=0.15))
        )
        
        result = await get_battery(mock_context)
        
        assert result["status"] == "success"
        assert "warning" in result["battery"]
        assert "low" in result["battery"]["warning"].lower()
    
    async def test_get_battery_medium_warning(self, mock_context, mock_drone):
        """Test medium battery warning at 20-30%"""
        mock_drone.telemetry.battery = MagicMock(
            return_value=make_async_iter(MockBattery(voltage_v=14.5, remaining_percent=0.25))
        )
        
        result = await get_battery(mock_context)
        
        assert result["status"] == "success"
        assert "warning" in result["battery"]
        assert "getting low" in result["battery"]["warning"].lower() or "consider landing" in result["battery"]["warning"].lower()
    
    async def test_get_battery_uncalibrated_estimation(self, mock_context, mock_drone):
        """Test battery estimation when percentage unavailable (0% with good voltage)"""
        mock_drone.telemetry.battery = MagicMock(
            return_value=make_async_iter(MockBattery(voltage_v=16.0, remaining_percent=0.0))
        )
        
        result = await get_battery(mock_context)
        
        assert result["status"] == "success"
        # Should provide estimated percentage when actual is 0 but voltage is good
        assert "estimated_percent" in result["battery"] or "note" in result["battery"]
    
    async def test_get_battery_connection_timeout(self, disconnected_context):
        """Test battery fails when drone not connected"""
        result = await get_battery(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_get_battery_exception(self, mock_context, mock_drone):
        """Test battery handles exceptions gracefully"""
        mock_drone.telemetry.battery = MagicMock(
            side_effect=Exception("Battery read failed")
        )
        
        result = await get_battery(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()

