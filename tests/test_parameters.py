"""
Integration tests for parameter management.

Connects to real SITL/drone - no mocks!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_parameters.py -v
"""
import pytest


class TestParameters:
    """Tests for reading/writing parameters"""
    
    async def test_read_float_parameter(self, drone):
        """Test reading a float parameter"""
        # RTL_ALT is a common float parameter (in cm for ArduPilot)
        value = await drone.param.get_param_float("RTL_ALT")
        
        assert value is not None
        assert value > 0
        print(f"📊 RTL_ALT = {value} cm ({value/100}m)")
    
    async def test_read_int_parameter(self, drone):
        """Test reading an integer parameter"""
        # FENCE_ENABLE is a common int parameter
        try:
            value = await drone.param.get_param_int("FENCE_ENABLE")
            print(f"📊 FENCE_ENABLE = {value}")
        except Exception:
            # Try another common parameter
            value = await drone.param.get_param_int("ARMING_CHECK")
            print(f"📊 ARMING_CHECK = {value}")
    
    async def test_write_parameter(self, drone):
        """Test writing a parameter (and restoring it)"""
        param_name = "RTL_ALT"
        
        # Read original value
        original = await drone.param.get_param_float(param_name)
        print(f"📊 Original {param_name} = {original}")
        
        # Write new value
        new_value = original + 100  # Add 1 meter
        await drone.param.set_param_float(param_name, new_value)
        print(f"📝 Set {param_name} = {new_value}")
        
        # Verify it changed
        current = await drone.param.get_param_float(param_name)
        assert abs(current - new_value) < 1, f"Parameter didn't change: {current} vs {new_value}"
        
        # Restore original
        await drone.param.set_param_float(param_name, original)
        print(f"🔄 Restored {param_name} = {original}")
        
        print("✅ Parameter read/write successful")
    
    async def test_get_all_params(self, drone):
        """Test getting all parameters"""
        params = await drone.param.get_all_params()
        
        # Count parameters
        int_count = len(params.int_params)
        float_count = len(params.float_params)
        total = int_count + float_count
        
        assert total > 100, f"Expected 100+ parameters, got {total}"
        print(f"📊 Found {total} parameters ({int_count} int, {float_count} float)")
