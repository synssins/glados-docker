# Import individual tools
from .do_nothing import tool_definition as do_nothing_def, DoNothing
from .get_report import tool_definition as get_report_def, GetReport
from .speak import tool_definition as speak_def, Speak
from .vision_look import tool_definition as vision_look_def, VisionLook
from .preferences import (
    get_preferences_definition,
    set_preference_definition,
    GetPreferences,
    SetPreference,
)
from .robot_move import tool_definition as robot_move_def, RobotMove
from .robot_status import tool_definition as robot_status_def, RobotStatus
from .robot_emergency_stop import tool_definition as robot_estop_def, RobotEmergencyStop

# slow_clap removed — requires sounddevice (local audio playback),
# not viable in a headless container.

# Export all tool definitions
tool_definitions = [
    do_nothing_def,
    get_report_def,
    speak_def,
    vision_look_def,
    get_preferences_definition,
    set_preference_definition,
    robot_move_def,
    robot_status_def,
    robot_estop_def,
]

# Export all tool classes
tool_classes = {
    "do_nothing": DoNothing,
    "get_report": GetReport,
    "speak": Speak,
    "vision_look": VisionLook,
    "get_preferences": GetPreferences,
    "set_preference": SetPreference,
    "robot_move": RobotMove,
    "robot_status": RobotStatus,
    "robot_emergency_stop": RobotEmergencyStop,
}

# Export all tool names
all_tools = list(tool_classes.keys())
