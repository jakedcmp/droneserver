# Quick Test (5 Minutes)

Fast validation of MAVLink MCP v1.2.3 features.

## Overview

- **Duration:** ~5 minutes
- **Tools Tested:** 15 out of 36
- **Complexity:** Low
- **Best For:** First-time setup verification or quick feature check

---

## ⚠️ CRITICAL SAFETY NOTE

**ALTITUDE REFERENCE:** All altitude commands in this test use **relative altitude** (height above home/ground), NOT absolute MSL altitude. The MCP server handles the conversion automatically.

If you modify this test, be aware:
- `takeoff`, `reposition`: Use relative altitude
- Home elevation may vary (e.g., 25m MSL in SITL)
- Never command altitudes below home elevation

---

## Copy This Prompt Into ChatGPT

```
Quick inspection test:

1. Show me all battery parameters
2. Get the RTL altitude parameter - if it's less than 20m, set it to 20m
3. Run a health check
4. Arm and takeoff to 10 meters
5. Face north (0 degrees) to orient the camera
6. Fly to lat 33.6459, lon -117.8427 staying at your current altitude
7. Face east (90 degrees) and hold position for 10 seconds
8. Reposition to lat 33.6460, lon -117.8428, climb to 20m relative altitude
9. Check battery level
10. IMPORTANT: Use upload_mission (NOT initiate_mission) to create a 3-waypoint mission going north, east, then back (don't start it)
11. Download the mission using download_mission to verify it was uploaded correctly
12. Check if any mission is currently running (should be false)
13. Return to launch and land
14. Disarm

Execute this step by step and report status after each action.

After completing all steps, create a summary report:
- Total steps completed: X/14
- Steps that succeeded: [list]
- Steps that failed: [list with reasons]
- Battery used: X%
- New features tested: [list which v1.2.0+ features were used]
- Overall success: YES/NO
- Key observations: [brief notes]
```

---

## What This Tests

### Parameter Management (v1.2.0)
- ✅ `list_parameters` - List battery parameters
- ✅ `get_parameter` - Check RTL altitude
- ✅ `set_parameter` - Modify RTL altitude if needed

### Advanced Navigation (v1.2.0)
- ✅ `set_yaw` - Face north (0°) and east (90°)
- ✅ `go_to_location` - Navigate to GPS coordinates
- ✅ `reposition` - Move to new position and hold

### Mission Enhancements (v1.2.0)
- ✅ `upload_mission` - Upload without starting
- ✅ `download_mission` - Verify upload
- ✅ `is_mission_finished` - Check mission status

### Basic Flight Control
- ✅ `get_health` - Pre-flight check
- ✅ `arm_drone` - Arm motors
- ✅ `takeoff_drone` - Climb to altitude
- ✅ `get_battery` - Battery monitoring
- ✅ `return_to_launch` - RTL
- ✅ `land_drone` - Landing
- ✅ `disarm_drone` - Disarm motors

---

## Expected Results

✅ **All 14 steps should complete successfully**

Common variations:
- Download mission may not be supported (some autopilots) - keep local copy
- Battery readings may be uncalibrated - set BATT_CAPACITY parameter
- Battery percentage may be estimated (uncalibrated sensor) - voltage still reported

---

## Success Criteria

- ✅ At least 10/13 steps complete successfully
- ✅ Parameter management works (steps 1-2)
- ✅ Basic flight works (steps 3-5, 12-13)
- ✅ At least 1 advanced navigation feature works (steps 6-7)
- ✅ Mission upload works (step 9)

---

## Troubleshooting

**If steps fail**, see:
- [TESTING_REFERENCE.md](TESTING_REFERENCE.md) - Common issues & solutions
- [TESTING_FIXES.md](TESTING_FIXES.md) - Known workarounds
- [STATUS.md](STATUS.md) - Feature compatibility

---

## Next Steps

**Test passed?** → Try [TESTING_COMPREHENSIVE.md](TESTING_COMPREHENSIVE.md) for full validation

**Test failed?** → Check [TESTING_REFERENCE.md](TESTING_REFERENCE.md) for troubleshooting

**Want detailed verification?** → Run [TESTING_GRANULAR.md](TESTING_GRANULAR.md)

---

[← Back to Testing Guide](TESTING_GUIDE.md)

