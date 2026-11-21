# Agent guide

You drive two real SO-101 arms: look at a camera frame, choose one small action, execute it, look again.
The task comes in your prompt. README.md is the manual.

- robotd (systemd user service) owns the arms and the one shared state. Use ONLY `from so101 import Robot` or the `robot` CLI.
  Never open the serial ports or the camera yourself; never start a second robotd.
- Step and look: `robot raw "move left_elbow_flex -5; wait; snap; state"`, then open snap_left.jpg / snap_right.jpg / snap_scene.jpg
  (wrist cameras: icSpring on the left arm, Sonix on the right; scene = Logitech C920). `robot cams` = which are connected.
- Humans may be hand-guiding (torque off) or using the panel at the same time: read `robot` (state) before each move.
- Safety: small steps; lift before any sideways move; first `goto` slow and small to confirm signs; never release torque raised.
- Develop and test without hardware: `SO101_SIM=1` (sim robotd + test_sim.py).
- Do not use Slack. Keep README.md short and current.
