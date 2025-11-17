#!/usr/bin/env python3
"""
Script to add enhanced logging to all MCP tools in mavlinkmcp.py
"""

import re

# Read the file
with open('src/server/mavlinkmcp.py', 'r') as f:
    content = f.read()

# Dictionary mapping tool names to their key MAVLink commands
tool_logging = {
    # Basic tools
    'arm_drone': [
        ('logger.info("Arming")', 'log_tool_call("arm_drone")'),
        ('await drone.action.arm()', 'log_mavlink_command("drone.action.arm")\n        await drone.action.arm()')
    ],
    'takeoff_drone': [
        ('logger.info("Initiating takeoff")', 'log_tool_call("takeoff_drone", takeoff_altitude=takeoff_altitude)'),
        ('await drone.action.set_takeoff_altitude(takeoff_altitude)', 
         'log_mavlink_command("drone.action.set_takeoff_altitude", altitude=takeoff_altitude)\n    await drone.action.set_takeoff_altitude(takeoff_altitude)'),
        ('await drone.action.takeoff()', 'log_mavlink_command("drone.action.takeoff")\n    await drone.action.takeoff()')
    ],
    'land': [
        ('logger.info("Initiating landing")', 'log_tool_call("land")'),
        ('await drone.action.land()', 'log_mavlink_command("drone.action.land")\n    await drone.action.land()')
    ],
    'get_position': [
        ('logger.info("Fetching drone position")', 'log_tool_call("get_position")\n    logger.info("Fetching drone position")')
    ],
    'move_to_relative': [
        ('logger.info(f"Moving in GUIDED mode:")', 
         'log_tool_call("move_to_relative", north_m=north_m, east_m=east_m, down_m=down_m, yaw_deg=yaw_deg)\n        logger.info(f"Moving in GUIDED mode:")'),
        ('await drone.action.goto_location(', 
         'log_mavlink_command("drone.action.goto_location", lat=target_lat, lon=target_lon, alt=target_alt, yaw=yaw_deg)\n        await drone.action.goto_location(')
    ]
}

# Apply replacements
for tool, replacements in tool_logging.items():
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new, 1)  # Replace only first occurrence
            print(f"✓ Updated {tool}: {old[:50]}...")
        else:
            print(f"✗ Could not find in {tool}: {old[:50]}...")

# Write back
with open('src/server/mavlinkmcp.py', 'w') as f:
    f.write(content)

print("\n✓ Done! Enhanced logging added.")

