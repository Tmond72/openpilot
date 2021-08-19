import math
from cereal import log
from selfdrive.controls.lib.latcontrol import LatControl, MIN_CTRL_SPEED


class LatControlAngle(LatControl):
  def update(self, active, CS, CP, VM, params, desired_curvature, desired_curvature_rate):
    angle_log = log.ControlsState.LateralAngleState.new_message()

    if CS.vEgo < MIN_CTRL_SPEED or not active:
      angle_log.active = False
      angle_steers_des = float(CS.steeringAngleDeg)
    else:
      angle_log.active = True
      angle_steers_des = math.degrees(VM.get_steer_from_curvature(-desired_curvature, CS.vEgo))
      angle_steers_des += params.angleOffsetDeg

    angle_log.saturated = False
    angle_log.steeringAngleDeg = angle_steers_des
    return 0, float(angle_steers_des), angle_log
