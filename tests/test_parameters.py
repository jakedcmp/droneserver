"""
Tests for parameter management tools:
- get_parameter
- set_parameter
- list_parameters
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))

from mavlinkmcp import (
    get_parameter,
    set_parameter,
    list_parameters,
)


class TestGetParameter:
    """Tests for get_parameter tool"""
    
    async def test_get_parameter_float_success(self, mock_context, mock_drone):
        """Test successful float parameter read"""
        mock_drone.param.get_param_float = AsyncMock(return_value=1500.0)
        
        result = await get_parameter(mock_context, name="RTL_ALT", param_type="float")
        
        assert result["status"] == "success"
        assert result["name"] == "RTL_ALT"
        assert result["value"] == 1500.0
        assert result["type"] == "float"
    
    async def test_get_parameter_int_success(self, mock_context, mock_drone):
        """Test successful int parameter read"""
        mock_drone.param.get_param_int = AsyncMock(return_value=5200)
        
        result = await get_parameter(mock_context, name="BATT_CAPACITY", param_type="int")
        
        assert result["status"] == "success"
        assert result["name"] == "BATT_CAPACITY"
        assert result["value"] == 5200
        assert result["type"] == "int"
    
    async def test_get_parameter_auto_detect_float(self, mock_context, mock_drone):
        """Test auto-detect finds float parameter"""
        mock_drone.param.get_param_float = AsyncMock(return_value=500.0)
        
        result = await get_parameter(mock_context, name="WPNAV_SPEED")
        
        assert result["status"] == "success"
        assert result["type"] == "float"
    
    async def test_get_parameter_auto_detect_int_fallback(self, mock_context, mock_drone):
        """Test auto-detect falls back to int when float fails"""
        mock_drone.param.get_param_float = AsyncMock(side_effect=Exception("Not a float"))
        mock_drone.param.get_param_int = AsyncMock(return_value=5200)
        
        result = await get_parameter(mock_context, name="BATT_CAPACITY")
        
        assert result["status"] == "success"
        assert result["type"] == "int"
        assert result["value"] == 5200
    
    async def test_get_parameter_not_found(self, mock_context, mock_drone):
        """Test parameter not found error"""
        mock_drone.param.get_param_float = AsyncMock(side_effect=Exception("Parameter not found"))
        mock_drone.param.get_param_int = AsyncMock(side_effect=Exception("Parameter not found"))
        
        result = await get_parameter(mock_context, name="INVALID_PARAM")
        
        assert result["status"] == "failed"
        assert "not found" in result["error"].lower() or "inaccessible" in result["error"].lower()
    
    async def test_get_parameter_suggests_list(self, mock_context, mock_drone):
        """Test that failed get suggests using list_parameters"""
        mock_drone.param.get_param_float = AsyncMock(side_effect=Exception("Not found"))
        mock_drone.param.get_param_int = AsyncMock(side_effect=Exception("Not found"))
        
        result = await get_parameter(mock_context, name="INVALID_PARAM")
        
        assert result["status"] == "failed"
        assert "list_parameters" in result.get("suggestion", "").lower()
    
    async def test_get_parameter_connection_timeout(self, disconnected_context):
        """Test get fails when drone not connected"""
        result = await get_parameter(disconnected_context, name="RTL_ALT")
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestSetParameter:
    """Tests for set_parameter tool"""
    
    async def test_set_parameter_float_success(self, mock_context, mock_drone):
        """Test successful float parameter set"""
        mock_drone.param.get_param_float = AsyncMock(return_value=1500.0)
        mock_drone.param.set_param_float = AsyncMock()
        
        result = await set_parameter(mock_context, name="RTL_ALT", value=2000.0, param_type="float")
        
        assert result["status"] == "success"
        assert result["name"] == "RTL_ALT"
        assert result["old_value"] == 1500.0
        assert result["new_value"] == 2000.0
        mock_drone.param.set_param_float.assert_called_once_with("RTL_ALT", 2000.0)
    
    async def test_set_parameter_int_success(self, mock_context, mock_drone):
        """Test successful int parameter set"""
        mock_drone.param.get_param_int = AsyncMock(return_value=5200)
        mock_drone.param.set_param_int = AsyncMock()
        
        result = await set_parameter(mock_context, name="BATT_CAPACITY", value=6000.0, param_type="int")
        
        assert result["status"] == "success"
        assert result["new_value"] == 6000
        mock_drone.param.set_param_int.assert_called_once_with("BATT_CAPACITY", 6000)
    
    async def test_set_parameter_auto_detect_int(self, mock_context, mock_drone):
        """Test auto-detect uses int for whole numbers"""
        mock_drone.param.get_param_int = AsyncMock(return_value=5200)
        mock_drone.param.set_param_int = AsyncMock()
        
        result = await set_parameter(mock_context, name="BATT_CAPACITY", value=6000.0)
        
        assert result["status"] == "success"
        assert result["type"] == "int"
    
    async def test_set_parameter_includes_reboot_warning(self, mock_context, mock_drone):
        """Test that set includes reboot warning"""
        mock_drone.param.get_param_float = AsyncMock(return_value=1500.0)
        mock_drone.param.set_param_float = AsyncMock()
        
        result = await set_parameter(mock_context, name="RTL_ALT", value=2000.0)
        
        assert result["status"] == "success"
        assert "reboot" in result.get("warning", "").lower()
    
    async def test_set_parameter_failure(self, mock_context, mock_drone):
        """Test parameter set failure"""
        # Need to make both int and float set fail, and also the get to fail
        # so the function can't determine old value
        mock_drone.param.get_param_float = AsyncMock(side_effect=Exception("Read failed"))
        mock_drone.param.get_param_int = AsyncMock(side_effect=Exception("Read failed"))
        mock_drone.param.set_param_float = AsyncMock(side_effect=Exception("Write failed"))
        mock_drone.param.set_param_int = AsyncMock(side_effect=Exception("Write failed"))
        
        result = await set_parameter(mock_context, name="RTL_ALT", value=2000.0)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()
    
    async def test_set_parameter_connection_timeout(self, disconnected_context):
        """Test set fails when drone not connected"""
        result = await set_parameter(disconnected_context, name="RTL_ALT", value=2000.0)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestListParameters:
    """Tests for list_parameters tool"""
    
    async def test_list_parameters_all(self, mock_context, mock_drone):
        """Test listing all parameters"""
        result = await list_parameters(mock_context)
        
        assert result["status"] == "success"
        assert "parameters" in result
        assert result["count"] > 0
        mock_drone.param.get_all_params.assert_called_once()
    
    async def test_list_parameters_includes_warning(self, mock_context, mock_drone):
        """Test that listing all includes warning about large list"""
        result = await list_parameters(mock_context)
        
        assert result["status"] == "success"
        assert "warning" in result
    
    async def test_list_parameters_with_filter(self, mock_context, mock_drone):
        """Test listing parameters with prefix filter"""
        result = await list_parameters(mock_context, filter_prefix="RTL")
        
        assert result["status"] == "success"
        assert result["filter"] == "RTL"
        # All returned params should start with RTL
        for param in result["parameters"]:
            assert param["name"].upper().startswith("RTL")
    
    async def test_list_parameters_filter_case_insensitive(self, mock_context, mock_drone):
        """Test that filter is case insensitive"""
        result = await list_parameters(mock_context, filter_prefix="rtl")
        
        assert result["status"] == "success"
        assert result["filter"] == "rtl"
        # Should still find RTL_ parameters
        for param in result["parameters"]:
            assert param["name"].upper().startswith("RTL")
    
    async def test_list_parameters_empty_filter(self, mock_context, mock_drone):
        """Test empty filter returns all parameters"""
        result = await list_parameters(mock_context, filter_prefix="")
        
        assert result["status"] == "success"
        assert result["count"] > 0
    
    async def test_list_parameters_includes_type(self, mock_context, mock_drone):
        """Test that parameters include type information"""
        result = await list_parameters(mock_context)
        
        assert result["status"] == "success"
        for param in result["parameters"]:
            assert "type" in param
            assert param["type"] in ["int", "float"]
    
    async def test_list_parameters_connection_timeout(self, disconnected_context):
        """Test list fails when drone not connected"""
        result = await list_parameters(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()
    
    async def test_list_parameters_exception(self, mock_context, mock_drone):
        """Test list handles exceptions gracefully"""
        mock_drone.param.get_all_params = AsyncMock(side_effect=Exception("Failed to get params"))
        
        result = await list_parameters(mock_context)
        
        assert result["status"] == "failed"
        assert "failed" in result["error"].lower()

