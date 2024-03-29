import rclpy
from rclpy.node import Node
from nav_msgs.msg import Odometry
import tf_transformations
import numpy as np
from localization_project.ekf import RobotEKF
from localization_project.motion_models import velocity_motion_model, odometry_motion_model
from localization_project.measurement_model import range_and_bearing, z_landmark, residual
from std_msgs.msg import Header
from geometry_msgs.msg import Pose, Twist  # useful?
from rclpy.qos import qos_profile_sensor_data


class EKF_node(Node):
    def __init__(self):
        super().__init__('EKF_node')

        # Subscriptions
        self.ground_truth_sub = self.create_subscription(
            Odometry, '/ground_truth', self.ground_truth_callback, qos_profile_sensor_data)
        self.odom_sub = self.create_subscription(
            Odometry, '/diff_drive_controller/odom', self.odometry_callback, qos_profile_sensor_data)
        self.vel_sub = self.create_subscription(
            Odometry, '/diff_drive_controller/odom', self.velocity_callback, qos_profile_sensor_data)

        # Publishers
        self.ekf_pub = self.create_publisher(Odometry, '/ekf', 10)

        # Parameters
        self.declare_parameters(
            namespace='',
            parameters=[
                ('ekf_period_s', 0.01),
                ('initial_pose', [-2.0, 0.0, 0.0]),
                ('initial_covariance', [
                 0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.01, 0.001]),
                ('landmarks', [-1.1, -1.1, -1.1, 0.0, -1.1, 1.1, 0.0, -
                 1.1, 0.0, 0.0, 0.0, 1.1, 1.1, -1.1, 1.1, 0.0, 1.1, 1.1]),
                ('std_rot1', 0.05),
                ('std_transl', 0.05),
                ('std_rot2', 0.05),
                ('std_lin_vel', 0.3),
                ('std_ang_vel', 1.5),
                # da consegna
                ('std_rng', 0.3),
                ('std_brg', np.deg2rad(1.0)),
                ('max_range', 8.0),
                ('fov_deg', np.deg2rad(45.0)),
            ]
        )

        self.ekf_period_s = self.get_parameter('ekf_period_s').value
        self.initial_pose = self.get_parameter('initial_pose').value
        self.initial_covariance = np.reshape(
            self.get_parameter('initial_covariance').value, (3, 3))
        self.lmark = self.get_parameter('landmarks').value
        self.std_rot1 = self.get_parameter('std_rot1').value
        self.std_transl = self.get_parameter('std_transl').value
        self.std_rot2 = self.get_parameter('std_rot2').value
        self.std_lin_vel = self.get_parameter('std_lin_vel').value
        self.std_ang_vel = self.get_parameter('std_ang_vel').value
        self.std_rng = self.get_parameter('std_rng').value
        self.std_brg = self.get_parameter('std_brg').value
        self.max_range = self.get_parameter('max_range').value
        self.fov_deg = self.get_parameter('fov_deg').value

        self.timer = self.create_timer(self.ekf_period_s, self.run_ekf)
        self.logging_timer = self.create_timer(1.0, self.log_callback)

        # Initialize other variables and EKF
        self.ground_truth = np.array([-2.0, 0.0, 0.0])
        self.x = self.initial_pose[0]
        self.y = self.initial_pose[1]
        self.theta = self.initial_pose[2]
        self.v = 1e-10  # 0.0001
        self.w = 1e-10  # 0.0001
        self.mu = np.zeros((3, 1))

        eval_hx, eval_Ht = range_and_bearing()
        eval_gux, eval_Gt, eval_Vt = velocity_motion_model()
        eval_gux_odom, eval_Gt_odom, eval_Vt_odom = odometry_motion_model()

        self.ekf = RobotEKF(dim_x=3, dim_z=2, dim_u=2,
                            eval_gux=eval_gux, eval_Gt=eval_Gt, eval_Vt=eval_Vt,
                            eval_hx=eval_hx, eval_Ht=eval_Ht)

        self.mu = np.array([self.initial_pose]).T  # x, y, theta
        self.Sigma = np.diag(self.initial_covariance)
        self.Mt = np.diag([self.std_lin_vel**2, self.std_ang_vel**2])
        self.Qt = np.diag([self.std_rng**2, self.std_brg**2])
        self.get_logger().info("EKF_node initiated")

    def odometry_callback(self, msgs):
        quat = [msgs.pose.pose.orientation.x, msgs.pose.pose.orientation.y,
                msgs.pose.pose.orientation.z, msgs.pose.pose.orientation.w]
        _, _, self.theta = tf_transformations.euler_from_quaternion(quat)
        self.x = msgs.pose.pose.position.x
        self.y = msgs.pose.pose.position.y

    def ground_truth_callback(self, msg):
        quat = [msg.pose.pose.orientation.x, msg.pose.pose.orientation.y,
                msg.pose.pose.orientation.z, msg.pose.pose.orientation.w]
        _, _, self.ground_truth[2] = tf_transformations.euler_from_quaternion(
            quat)
        self.ground_truth[0] = msg.pose.pose.position.x
        self.ground_truth[1] = msg.pose.pose.position.y

    def velocity_callback(self, msgs):
        self.v = msgs.twist.twist.linear.x
        self.w = msgs.twist.twist.angular.z

    def run_ekf(self):
        # Perform prediction step
        self.get_logger().info("Prediction Step started")
        self.ekf.predict(u=np.array([[self.v, self.w]]).T,
                         g_extra_args=[self.ekf_period_s])

        self.get_logger().info("Update Step started")
        for i in range(0, len(self.lmark), 2):
            lmark = [self.lmark[i], self.lmark[i + 1]]
            z = z_landmark(np.array([self.ground_truth]).T, lmark, self.ekf.eval_hx,
                           self.std_rng, self.std_brg)

            if z is not None:
                self.z = z
                # Perform update step
                self.ekf.update(self.z, lmark, residual=np.subtract)
        # Publish the result

        # positions
        ekf_estimate = self.ekf.mu
        ekf_msg = Odometry()
        ekf_msg.pose.pose.position.x = ekf_estimate[0, 0]
        ekf_msg.pose.pose.position.y = ekf_estimate[1, 0]
        ekf_msg.pose.pose.orientation.z = ekf_estimate[2, 0]
        ekf_msg.twist.twist.linear.x = self.v
        ekf_msg.twist.twist.angular.z = self.w

        # covariance manipulation
        cov_x, cov_y, cov_theta = self.ekf.Sigma[0,
                                                 0], self.ekf.Sigma[1, 1], self.ekf.Sigma[2, 2]
        ekf_msg.pose.covariance[0] = cov_x
        ekf_msg.pose.covariance[7] = cov_y
        ekf_msg.pose.covariance[35] = cov_theta

        # Header
        ekf_msg.header = Header()
        ekf_msg.header.stamp = self.get_clock().now().to_msg()
        ekf_msg.header.frame_id = 'odom'
        ekf_msg.child_frame_id = 'base_footprint'

        # PUBLICATION
        self.ekf_pub.publish(ekf_msg)
        self.get_logger().info(f'Publishing ekf_msg: {ekf_msg}')

    # logging to track positions and velocities
    def log_callback(self):
        self.get_logger().info(f'calculating velocities: {self.v, self.w}')
        self.get_logger().info(
            f'calculating positions: {self.x, self.y, self.theta}')
        self.get_logger().info(
            f'ground truth positions: {self.ground_truth[0], self.ground_truth[1], self.ground_truth[2]}')


def main(args=None):
    rclpy.init(args=args)
    kalman_filter = EKF_node()
    rclpy.spin(kalman_filter)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
