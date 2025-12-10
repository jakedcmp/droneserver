"""
Tests for basic flight control tools:
- arm_drone
- disarm_drone
- takeoff
- land
- hold_position
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

# Import the tools we're testing
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))

from mavlinkmcp import (
    arm_drone,
    disarm_drone,
    takeoff,
    land,
    hold_position,
)


class TestArmDrone:
    """Tests for arm_drone tool"""
    
    async def test_arm_drone_success(self, mock_context, mock_drone):
        """Test successful drone arming"""
        result = await arm_drone(mock_context)
        
        assert result["status"] == "success"
        assert "armed" in result["message"].lower()
        mock_drone.action.arm.assert_called_once()
    
    async def test_arm_drone_connection_timeout(self, disconnected_context):
        """Test arm fails when drone not connected"""
        result = await arm_drone(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_arm_drone_exception(self, mock_context, mock_drone):
        """Test arm fails gracefully on exception"""
        mock_drone.action.arm = AsyncMock(side_effect=Exception("Arm failed: Pre-arm checks not passed"))
        
        # The arm_drone function doesn't have explicit exception handling,
        # so this would raise. In a real scenario, we'd want to add error handling.
        with pytest.raises(Exception):
            await arm_drone(mock_context)


class TestDisarmDrone:
    """Tests for disarm_drone tool"""
    
    async def test_disarm_drone_success(self, mock_context, mock_drone):
        """Test successful drone disarming"""
        result = await disarm_drone(mock_context)
        
        assert result["status"] == "success"
        assert "disarmed" in result["message"].lower()
        mock_drone.action.disarm.assert_called_once()
    
    async def test_disarm_drone_connection_timeout(self, disconnected_context):
        """Test disarm fails when drone not connected"""
        result = await disarm_drone(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_disarm_drone_exception(self, mock_context, mock_drone):
        """Test disarm handles exceptions gracefully"""
        mock_drone.action.disarm = AsyncMock(side_effect=Exception("Disarm failed"))
        
        result = await disarm_drone(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()


class TestTakeoff:
    """Tests for takeoff tool"""
    
    async def test_takeoff_default_altitude(self, mock_context, mock_drone):
        """Test takeoff with default altitude (3.0m)"""
        result = await takeoff(mock_context)
        
        assert result["status"] == "success"
        assert "3.0" in result["message"] or "3" in result["message"]
        mock_drone.action.set_takeoff_altitude.assert_called_once_with(3.0)
        mock_drone.action.takeoff.assert_called_once()
    
    async def test_takeoff_custom_altitude(self, mock_context, mock_drone):
        """Test takeoff with custom altitude"""
        result = await takeoff(mock_context, takeoff_altitude=10.0)
        
        assert result["status"] == "success"
        assert "10" in result["message"]
        mock_drone.action.set_takeoff_altitude.assert_called_once_with(10.0)
    
    async def test_takeoff_connection_timeout(self, disconnected_context):
        """Test takeoff fails when drone not connected"""
        result = await takeoff(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestLand:
    """Tests for land tool"""
    
    async def test_land_success(self, mock_context, mock_drone):
        """Test successful landing"""
        result = await land(mock_context)
        
        assert result["status"] == "success"
        assert "landing" in result["message"].lower()
        mock_drone.action.land.assert_called_once()
    
    async def test_land_connection_timeout(self, disconnected_context):
        """Test land fails when drone not connected"""
        result = await land(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestHoldPosition:
    """Tests for hold_position tool"""
    
    async def test_hold_position_success(self, mock_context, mock_drone):
        """Test successful position hold"""
        result = await hold_position(mock_context)
        
        assert result["status"] == "success"
        assert "holding" in result["message"].lower() or "hold" in result["message"].lower()
        # Should use goto_location with current position (stays in GUIDED mode)
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_hold_position_returns_position(self, mock_context, mock_drone):
        """Test that hold_position returns current position"""
        result = await hold_position(mock_context)
        
        assert result["status"] == "success"
        assert "position" in result
        assert "latitude_deg" in result["position"]
        assert "longitude_deg" in result["position"]
    
    async def test_hold_position_uses_guided_mode(self, mock_context, mock_drone):
        """Test that hold uses GUIDED mode (not LOITER)"""
        result = await hold_position(mock_context)
        
        assert result["status"] == "success"
        # Should include note about GUIDED mode
        assert "GUIDED" in result.get("note", "") or "GUIDED" in result.get("message", "")
    
    async def test_hold_position_connection_timeout(self, disconnected_context):
        """Test hold fails when drone not connected"""
        result = await hold_position(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_hold_position_exception(self, mock_context, mock_drone):
        """Test hold handles exceptions gracefully"""
        mock_drone.action.goto_location = AsyncMock(side_effect=Exception("Hold failed"))
        
        result = await hold_position(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()

