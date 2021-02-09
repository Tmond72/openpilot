from cereal import car
from common.numpy_fast import clip, interp
from selfdrive.car import apply_toyota_steer_torque_limits, create_gas_command, make_can_msg
from selfdrive.car.toyota.toyotacan import create_steer_command, create_ui_command, \
                                           create_accel_command, create_acc_cancel_command, \
                                           create_fcw_command
from selfdrive.car.toyota.values import Ecu, CAR, STATIC_MSGS, NO_STOP_TIMER_CAR, MIN_ACC_SPEED, SteerLimitParams
from opendbc.can.packer import CANPacker
from common.op_params import opParams
from selfdrive.car.toyota.accel_to_gas import predict as accel_to_gas

VisualAlert = car.CarControl.HUDControl.VisualAlert

# Accel limits
ACCEL_HYST_GAP = 0.02  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 1.5  # 1.5 m/s2
ACCEL_MIN = -3.0  # 3   m/s2
ACCEL_SCALE = max(ACCEL_MAX, -ACCEL_MIN)

def accel_hysteresis(accel, accel_steady, enabled):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if not enabled:
    # send 0 when disabled, otherwise acc faults
    accel_steady = 0.
  elif accel > accel_steady + ACCEL_HYST_GAP:
    accel_steady = accel - ACCEL_HYST_GAP
  elif accel < accel_steady - ACCEL_HYST_GAP:
    accel_steady = accel + ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady


def coast_accel(speed):  # given a speed, output coasting acceleration
  points = [[0, .504], [1.697, .266],
            [2.839, -.187], [3.413, -.233],
            [MIN_ACC_SPEED, -.145]]
  return interp(speed, *zip(*points))


def compute_gb_gas_interceptor(accel, speed):
  # This converts desired positive acceleration at a speed to gas percentage
  # It's only accurate up to MIN_ACC_SPEED (19 mph) for now since the function was fitted on data up to that speed
  # Once we reach that speed, we switch to sending acceleration anyway so this isn't a problem


  # def accel_to_gas(accel, speed):  # given a speed and acceleration, output gas percentage for pedal
  #   # averages x mae from 0 to 25 mph
  #   # poly, accel_coef = [-0.00036742174834669726, 0.022650177688971165, -0.05711605265453139], 0.14408144749460255
  #   # return (poly[0] * speed ** 2 + poly[1] * speed + poly[2]) + (accel_coef * accel)
  #   _c1, _c2, _c3, _c4 = [0.04412016647510183, 0.018224465923095633, 0.09983653162564889, 0.08837909527049172]
  #   return (accel * _c1 + (_c4 * (speed * _c2 + 1))) * (speed * _c3 + 1)

  return accel_to_gas([accel, speed])[0]


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.last_steer = 0
    self.accel_steady = 0.
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.op_params = opParams()
    self.standstill_hack = self.op_params.get('standstill_hack')

    self.steer_rate_limited = False

    self.fake_ecus = set()
    if CP.enableCamera:
      self.fake_ecus.add(Ecu.fwdCamera)
    if CP.enableDsu:
      self.fake_ecus.add(Ecu.dsu)

    self.packer = CANPacker(dbc_name)

  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, hud_alert,
             left_line, right_line, lead, left_lane_depart, right_lane_depart):

    # *** compute control surfaces ***

    # gas and brake

    apply_accel = actuators.gas - actuators.brake
    apply_gas = 0.

    min_pedal_accel = coast_accel(CS.out.vEgo)
    if self.op_params.get('min_pedal_accel') is not None:
      min_pedal_accel = self.op_params.get('min_pedal_accel')

    if CS.CP.enableGasInterceptor and CS.out.vEgo < MIN_ACC_SPEED:
      # send only negative accel if interceptor is detected. otherwise, send the regular value
      # +0.06 offset to reduce ABS pump usage when OP is engaged
      if apply_accel * ACCEL_SCALE > min_pedal_accel:
        apply_gas = compute_gb_gas_interceptor(apply_accel * ACCEL_SCALE, CS.out.vEgo)
      elif apply_accel <= 0:  # if not higher than coast accel, but not less than 0, send positive accel so car can decide how much to brake/coast
        apply_accel = 0.06 - actuators.brake  # only send negative accel and offset abs if pedal, under 19 mph, and commanding true brake

    apply_accel, self.accel_steady = accel_hysteresis(apply_accel, self.accel_steady, enabled)
    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)

    if self.op_params.get('apply_gas') is not None:
      apply_gas = self.op_params.get('apply_gas')
      apply_accel = 0.

    # steer torque
    new_steer = int(round(actuators.steer * SteerLimitParams.STEER_MAX))
    apply_steer = apply_toyota_steer_torque_limits(new_steer, self.last_steer, CS.out.steeringTorqueEps, SteerLimitParams)
    self.steer_rate_limited = new_steer != apply_steer

    # Cut steering for duration of known fault
    if not enabled or CS.steer_state in [9, 25] or (abs(CS.out.steeringRate) >= 100 and self.op_params.get('steer_fault_fix')):
      apply_steer = 0
      apply_steer_req = 0
    else:
      apply_steer_req = 1

    if not enabled and CS.pcm_acc_status:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    if CS.out.standstill and not self.last_standstill and CS.CP.carFingerprint not in NO_STOP_TIMER_CAR and not self.standstill_hack:
      self.standstill_req = True
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_steer = apply_steer
    self.last_accel = apply_accel
    self.last_standstill = CS.out.standstill

    can_sends = []

    #*** control msgs ***
    #print("steer {0} {1} {2} {3}".format(apply_steer, min_lim, max_lim, CS.steer_torque_motor)

    # toyota can trace shows this message at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    if Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_steer_command(self.packer, apply_steer, apply_steer_req, frame))

      # LTA mode. Set ret.steerControlType = car.CarParams.SteerControlType.angle and whitelist 0x191 in the panda
      # if frame % 2 == 0:
      #   can_sends.append(create_steer_command(self.packer, 0, 0, frame // 2))
      #   can_sends.append(create_lta_steer_command(self.packer, actuators.steerAngle, apply_steer_req, frame // 2))

    # we can spam can to cancel the system even if we are using lat only control
    if (frame % 3 == 0 and CS.CP.openpilotLongitudinalControl) or (pcm_cancel_cmd and Ecu.fwdCamera in self.fake_ecus):
      lead = lead or CS.out.vEgo < 12.    # at low speed we always assume the lead is present do ACC can be engaged

      # Lexus IS uses a different cancellation message
      if pcm_cancel_cmd and CS.CP.carFingerprint == CAR.LEXUS_IS:
        can_sends.append(create_acc_cancel_command(self.packer))
      elif CS.CP.openpilotLongitudinalControl:
        can_sends.append(create_accel_command(self.packer, apply_accel, pcm_cancel_cmd, self.standstill_req, lead))
      else:
        can_sends.append(create_accel_command(self.packer, 0, pcm_cancel_cmd, False, lead))

    if frame % 2 == 0 and CS.CP.enableGasInterceptor:
      # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
      # This prevents unexpected pedal range rescaling
      can_sends.append(create_gas_command(self.packer, apply_gas, frame//2))

    # ui mesg is at 100Hz but we send asap if:
    # - there is something to display
    # - there is something to stop displaying
    fcw_alert = hud_alert == VisualAlert.fcw
    steer_alert = hud_alert == VisualAlert.steerRequired

    send_ui = False
    if ((fcw_alert or steer_alert) and not self.alert_active) or \
       (not (fcw_alert or steer_alert) and self.alert_active):
      send_ui = True
      self.alert_active = not self.alert_active
    elif pcm_cancel_cmd:
      # forcing the pcm to disengage causes a bad fault sound so play a good sound instead
      send_ui = True

    if (frame % 100 == 0 or send_ui) and Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_ui_command(self.packer, steer_alert, pcm_cancel_cmd, left_line, right_line, left_lane_depart, right_lane_depart))

    if frame % 100 == 0 and Ecu.dsu in self.fake_ecus:
      can_sends.append(create_fcw_command(self.packer, fcw_alert))

    #*** static msgs ***

    for (addr, ecu, cars, bus, fr_step, vl) in STATIC_MSGS:
      if frame % fr_step == 0 and ecu in self.fake_ecus and CS.CP.carFingerprint in cars:
        can_sends.append(make_can_msg(addr, vl, bus))

    return can_sends
