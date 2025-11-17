#!/usr/bin/env python3
"""
Comprehensive script to add logging to ALL MCP tools
"""

import re

# Read the file
with open('src/server/mavlinkmcp.py', 'r') as f:
    lines = f.readlines()

# Track changes
changes_made = []

# Process each line
i = 0
while i < len(lines):
    line = lines[i]
    
    # Check if this is a tool definition
    if line.strip() == '@mcp.tool()':
        # Next line should be the function signature
        if i + 1 < len(lines):
            func_line = lines[i + 1]
            
            # Extract function name and parameters
            match = re.match(r'async def (\w+)\(ctx: Context(?:, (.+))?\) -> dict:', func_line)
            if match:
                func_name = match.group(1)
                params = match.group(2) if match.group(2) else ""
                
                # Skip to the docstring end and find the first actual code line
                j = i + 2
                while j < len(lines):
                    stripped = lines[j].strip()
                    
                    # Skip docstrings and empty lines
                    if stripped.startswith('"""') or stripped == '"""' or stripped == '' or stripped.startswith('#'):
                        j += 1
                        continue
                    
                    # Found first code line - check if it already has log_tool_call
                    if 'log_tool_call' not in lines[j]:
                        # Get the indentation
                        indent = len(lines[j]) - len(lines[j].lstrip())
                        indent_str = ' ' * indent
                        
                        # Build parameters for logging (exclude 'ctx')
                        param_list = []
                        if params:
                            for param in params.split(','):
                                param = param.strip()
                                if '=' in param:
                                    param_name = param.split('=')[0].strip().split(':')[0].strip()
                                else:
                                    param_name = param.split(':')[0].strip()
                                if param_name != 'ctx':
                                    param_list.append(f'{param_name}={param_name}')
                        
                        # Create log_tool_call line
                        if param_list:
                            log_line = f'{indent_str}log_tool_call("{func_name}", {", ".join(param_list)})\n'
                        else:
                            log_line = f'{indent_str}log_tool_call("{func_name}")\n'
                        
                        # Insert the log line
                        lines.insert(j, log_line)
                        changes_made.append(f"Added log_tool_call to {func_name}")
                    
                    break
                    j += 1
    
    # Look for MAVLink commands and add logging before them
    if 'await drone.action.' in line and 'log_mavlink_command' not in lines[i-1] if i > 0 else True:
        # Extract the MAVLink command
        match = re.search(r'await drone\.(\w+)\.(\w+)\((.*?)\)', line)
        if match:
            plugin = match.group(1)
            command = match.group(2)
            args = match.group(3)
            
            # Get indentation
            indent = len(line) - len(line.lstrip())
            indent_str = ' ' * indent
            
            # Create log line
            log_line = f'{indent_str}log_mavlink_command("drone.{plugin}.{command}")\n'
            
            # Insert before the command
            lines.insert(i, log_line)
            changes_made.append(f"Added log for drone.{plugin}.{command}")
            i += 1  # Skip the line we just inserted
    
    i += 1

# Write back
with open('src/server/mavlinkmcp.py', 'w') as f:
    f.writelines(lines)

print(f"✓ Done! Made {len(changes_made)} changes:")
for change in changes_made[:20]:  # Show first 20
    print(f"  - {change}")
if len(changes_made) > 20:
    print(f"  ... and {len(changes_made) - 20} more")

