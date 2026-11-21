"""Kinematics for one SO-101 arm (joint degrees <-> gripper tip xyz in m, arm base frame)."""
import os, numpy as np, logging
logging.disable(logging.WARNING)
from lerobot.model.kinematics import RobotKinematics
J = ["shoulder_pan","shoulder_lift","elbow_flex","wrist_flex","wrist_roll"]
K = RobotKinematics(os.path.join(os.path.dirname(os.path.abspath(__file__)), "so101.urdf"), "gripper_frame_link", J)
REST = np.array([1.3,-101.8,96.7,66.6,-6.5])
R_DOWN = K.forward_kinematics(REST)[:3,:3].copy()
def fk(q): return K.forward_kinematics(np.asarray(q,float))[:3,3]
def ik(q0, xyz, ow=0.5, iters=40):
    a = np.arctan2(xyz[1], xyz[0]); c, s_ = np.cos(a), np.sin(a)
    Rz = np.array([[c,-s_,0],[s_,c,0],[0,0,1]])
    T = np.eye(4); T[:3,:3] = Rz @ R_DOWN; T[:3,3] = xyz
    q = K.inverse_kinematics(np.asarray(q0,float), T, 1.0, ow, iters)
    return q, np.linalg.norm(fk(q) - np.asarray(xyz))
