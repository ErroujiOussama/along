"""List which servo IDs answer on each serial adapter (robotd must be stopped)."""
import glob, os
from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
for port in sorted(glob.glob('/dev/serial/by-id/*')):
    tag = port.split('Serial_')[1][:10]
    if not os.access(port, os.R_OK | os.W_OK):
        print(tag, 'NO PERMISSION'); continue
    motors = {f"m{i}": Motor(i, "sts3215", MotorNormMode.RANGE_M100_100) for i in range(1, 13)}
    bus = FeetechMotorsBus(port=port, motors=motors)
    bus.connect(handshake=False)
    print(tag, {i: bus.ping(i) for i in range(1, 13)})
    bus.disconnect(disable_torque=False)
