try:
    import cPickle as pickle
except ModuleNotFoundError:
    import pickle

import os
import json
import socket
from multiprocessing import Process, Queue
import time
import math
import numpy as np
import cv2
import cv2.aruco as aruco
from marker_detection_settings import SINGLE_DETECTION, CUBE_DETECTION


class TrackingScheduler:
    def __init__(self, start_tracking, stop_tracking):
        self.start_tracking = start_tracking
        self.stop_tracking = stop_tracking

    def main(self):

        while True:
            self.start_tracking.wait()
            self.start_tracking.clear()

            tracking_config = TrackingCofig.persisted()
            queue = Queue(1)
            filtered_queue = Queue(1)

            client_process = Process(target=DataPublishClientUDP(
                server_ip=tracking_config.server_ip,
                server_port=int(tracking_config.server_port),
                queue=queue,
                filtered_queue=filtered_queue
            ).listen)
            client_process.start()

            tracking_process = Process(target=Tracking(
                queue=queue,
                filtered_queue=filtered_queue,
                device_number=tracking_config.device_number,
                device_parameters_dir=tracking_config.device_parameters_dir,
                show_video=tracking_config.show_video,
                marker_detection_settings=tracking_config.marker_detection_settings,
                translation_offset=tracking_config.translation_offset).track)
            tracking_process.start()

            while True:
                time.sleep(1)

                if not tracking_process.is_alive():
                    client_process.terminate()
                    self.stop_tracking.clear()
                    break

                if self.stop_tracking.wait(0):
                    tracking_process.terminate()
                    client_process.terminate()
                    self.stop_tracking.clear()
                    break


class Tracking:
    def __init__(self, queue, filtered_queue, device_number, device_parameters_dir, show_video, marker_detection_settings, translation_offset):
        self.__data_queue = queue
        self.__filtered_data_queue = filtered_queue
        self.__device_number = device_number
        self.__device_parameters_dir = device_parameters_dir
        self.__show_video = show_video
        self.__marker_detection_settings = marker_detection_settings
        self.__translation_offset = translation_offset

    def track(self):
        #Descomentar quando nao for utilizar o DroidCam
        #video_capture = cv2.VideoCapture(
            #self.__device_number, cv2.CAP_DSHOW)
        video_capture = cv2.VideoCapture(
            self.__device_number)

        video_capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        video_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        detection_result = {}
        filtered_detection_result = {}
        kalman_filter = create_kalman_filter(9, 3, 0.0334)
        while True:
            _, frame = video_capture.read()

            if self.__marker_detection_settings.identifier == SINGLE_DETECTION:
                detection_result, filtered_detection_result = self.__single_marker_detection(frame, kalman_filter, filtered_detection_result)
            elif self.__marker_detection_settings.identifier == CUBE_DETECTION:
                detection_result, filtered_detection_result = self.__markers_cube_detection(frame, kalman_filter, filtered_detection_result)
            else:
                raise Exception("Invalid detection identifier. Received: {}".format(
                    self.__marker_detection_settings.identifier))

            self.__publish_coordinates(json.dumps(detection_result), json.dumps(filtered_detection_result))

            if self.__show_video:
                self.__show_video_result(frame, filtered_detection_result)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        video_capture.release()
        cv2.destroyAllWindows()

    def __single_marker_detection(self, frame, filter, last_detection_result):

        corners, ids = self.__detect_markers(frame)

        marker_rvec = None
        marker_tvec = None
        if np.all(ids is not None):
            marker_found = False
            marker_index = None

            for i in range(0, ids.size):
                if ids[i][0] == self.__marker_detection_settings.marker_id:
                    marker_found = True
                    marker_index = i
                    break

            if marker_found:
                cam_mtx, dist = self.__camera_parameters()
                rvecs, tvecs, _ = aruco.estimatePoseSingleMarkers(
                    corners, float(self.__marker_detection_settings.marker_length), cam_mtx, dist)

                marker_position = self.__get_position_matrix(
                    rvecs[marker_index], tvecs[marker_index])

                marker_position = self.__apply_transformation(
                    marker_position, self.__translation_offset)

                marker_rvec, marker_tvec = self.__get_rvec_and_tvec(
                    marker_position)

                aruco.drawAxis(frame, cam_mtx, dist,
                               marker_rvec, marker_tvec, 5)

        return self.__detection_result(marker_rvec, marker_tvec, filter, last_detection_result)

    def __markers_cube_detection(self, frame, filter, last_detection_result):
        corners, ids = self.__detect_markers(frame)

        main_marker_rvec = None
        main_marker_tvec = None
        if np.all(ids is not None):

            cam_mtx, dist = self.__camera_parameters()
            rvecs, tvecs, _ = aruco.estimatePoseSingleMarkers(
                corners, float(self.__marker_detection_settings.markers_length), cam_mtx, dist)

            choosen_marker_index = 0
            choosen_marker_id = ids[0][0]
            for i in range(0, ids.size):
                if tvecs[choosen_marker_index][0][2] > tvecs[i][0][2]:
                    choosen_marker_id = ids[i][0]
                    choosen_marker_index = i

            choosen_marker_position = self.__get_position_matrix(
                rvecs[choosen_marker_index], tvecs[choosen_marker_index])

            if choosen_marker_id != self.__marker_detection_settings.up_marker_id:
                choosen_marker_position = self.__apply_transformation(
                    choosen_marker_position, self.__marker_detection_settings.transformations[choosen_marker_id])

            choosen_marker_position = self.__apply_transformation(
                choosen_marker_position, self.__translation_offset)

            main_marker_rvec, main_marker_tvec = self.__get_rvec_and_tvec(
                choosen_marker_position)

            aruco.drawAxis(frame, cam_mtx, dist,
                           main_marker_rvec, main_marker_tvec, 5)

        return self.__detection_result(main_marker_rvec, main_marker_tvec, filter, last_detection_result)

    def __detect_markers(self, frame):
        parameters = aruco.DetectorParameters_create()
        parameters.adaptiveThreshConstant = 7
        parameters.cornerRefinementMethod = aruco.CORNER_REFINE_CONTOUR

        corners, ids, _ = aruco.detectMarkers(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
            aruco.Dictionary_get(aruco.DICT_6X6_250),
            parameters=parameters)

        aruco.drawDetectedMarkers(frame, corners)

        return corners, ids

    def __camera_parameters(self):
        cam_mtx = np.load(
            "{}/cam_mtx.npy".format(self.__device_parameters_dir))
        dist = np.load(
            "{}/dist.npy".format(self.__device_parameters_dir))

        return cam_mtx, dist

    def __get_position_matrix(self, rvec, tvec):
        rot_mtx = np.zeros(shape=(3, 3))
        cv2.Rodrigues(rvec, rot_mtx)

        position = np.concatenate(
            (rot_mtx, np.transpose(tvec)), axis=1)
        position = np.concatenate(
            (position, np.array([[0, 0, 0, 1]])))

        return position

    def __get_rvec_and_tvec(self, position_matrix):

        tvec_t = np.delete(position_matrix[:, 3], (3))

        position_matrix = np.delete(
            position_matrix, 3, 0)
        position_matrix = np.delete(
            position_matrix, 3, 1)

        rvec_t, _ = cv2.Rodrigues(position_matrix)

        return rvec_t.T, tvec_t.T

    def __apply_transformation(self, position_matrix, transformation):
        return np.dot(position_matrix, transformation)

    def __detection_result(self, rvec, tvec, filter, last_detection_result):
        filtered_detection_result = {}
        detection_result = {}

        filtered_detection_result['timestamp'] = time.time()
        detection_result['timestamp'] = filtered_detection_result['timestamp']
        last_detection_result['timestamp'] = filtered_detection_result['timestamp']

        success = rvec is not None and tvec is not None
        filtered_detection_result['success'] = success
        detection_result['success'] = success

        if success:
            rot_mtx = np.zeros(shape=(3, 3))
            cv2.Rodrigues(rvec, rot_mtx)

            filtered_detection_result['translation_x'] = tvec.item(0)
            filtered_detection_result['translation_y'] = tvec.item(1)
            filtered_detection_result['translation_z'] = tvec.item(2)
            filtered_detection_result['rotation_right_x'] = rot_mtx.item(0, 0)
            filtered_detection_result['rotation_right_y'] = rot_mtx.item(1, 0)
            filtered_detection_result['rotation_right_z'] = rot_mtx.item(2, 0)
            filtered_detection_result['rotation_up_x'] = rot_mtx.item(0, 1)
            filtered_detection_result['rotation_up_y'] = rot_mtx.item(1, 1)
            filtered_detection_result['rotation_up_z'] = rot_mtx.item(2, 1)
            filtered_detection_result['rotation_forward_x'] = rot_mtx.item(0, 2)
            filtered_detection_result['rotation_forward_y'] = rot_mtx.item(1, 2)
            filtered_detection_result['rotation_forward_z'] = rot_mtx.item(2, 2)

            detection_result['translation_x'] = tvec.item(0)
            detection_result['translation_y'] = tvec.item(1)
            detection_result['translation_z'] = tvec.item(2)
            detection_result['rotation_right_x'] = rot_mtx.item(0, 0)
            detection_result['rotation_right_y'] = rot_mtx.item(1, 0)
            detection_result['rotation_right_z'] = rot_mtx.item(2, 0)
            detection_result['rotation_up_x'] = rot_mtx.item(0, 1)
            detection_result['rotation_up_y'] = rot_mtx.item(1, 1)
            detection_result['rotation_up_z'] = rot_mtx.item(2, 1)
            detection_result['rotation_forward_x'] = rot_mtx.item(0, 2)
            detection_result['rotation_forward_y'] = rot_mtx.item(1, 2)
            detection_result['rotation_forward_z'] = rot_mtx.item(2, 2)

            measurements = create_measurement_matrix(filtered_detection_result)
            update_detection_result(filter, measurements, filtered_detection_result)

            if last_detection_result.get('success', False):
                detection_list = list(zip(filtered_detection_result.values(), last_detection_result.values()))
                i = 5
                oscillation = True
                while i < 14:
                    if abs(detection_list[i][0] - detection_list[i][1]) > 0.01:
                        oscillation = False
                        break
                    i += 1

                if oscillation:
                    self.__change_rot_mtx(filtered_detection_result, last_detection_result)

        
        return detection_result, filtered_detection_result

    def __publish_coordinates(self, data, filtered_data):
        if self.__data_queue.full():
            self.__data_queue.get()

        self.__data_queue.put(data)

        #if self.__filtered_data_queue.full():
        #    self.__filtered_data_queue.get()

        #self.__filtered_data_queue.put(filtered_data)

    def __show_video_result(self, frame, detection_result):
        win_name = "Tracking"
        cv2.namedWindow(win_name, cv2.WND_PROP_FULLSCREEN)
        cv2.setWindowProperty(
            win_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        font_color = (0, 255, 0)

        cv2.putText(frame, 'timestamp: {}'.format(detection_result['timestamp']), (0, 20),
                    font, font_scale, font_color, 2, cv2.LINE_AA)
        cv2.putText(frame, 'success: {}'.format(detection_result['success']), (0, 40),
                    font, font_scale, font_color, 2, cv2.LINE_AA)

        if detection_result['success'] == 1:
            cv2.putText(frame, 'translation_x: {:.2f}'.format(detection_result['translation_x']), (0, 60),
                        font, font_scale, font_color, 2, cv2.LINE_AA)     
            cv2.putText(frame, 'translation_y: {:.2f}'.format(detection_result['translation_y']), (0, 80),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'translation_z: {:.2f}'.format(detection_result['translation_z']), (0, 100),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_right_x: {:.2f}'.format(detection_result['rotation_right_x']), (0, 120),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_right_y: {:.2f}'.format(detection_result['rotation_right_y']), (0, 140),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_right_z: {:.2f}'.format(detection_result['rotation_right_z']), (0, 160),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_up_x: {:.2f}'.format(detection_result['rotation_up_x']), (0, 180),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_up_y: {:.2f}'.format(detection_result['rotation_up_y']), (0, 200),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_up_z: {:.2f}'.format(detection_result['rotation_up_z']), (0, 220),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_forward_x: {:.2f}'.format(detection_result['rotation_forward_x']), (0, 240),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_forward_y: {:.2f}'.format(detection_result['rotation_forward_y']), (0, 260),
                        font, font_scale, font_color, 2, cv2.LINE_AA)
            cv2.putText(frame, 'rotation_forward_z: {:.2f}'.format(detection_result['rotation_forward_z']), (0, 280),
                        font, font_scale, font_color, 2, cv2.LINE_AA)

        cv2.putText(frame, "Q - Quit ", (0, 305), font,
                    font_scale, font_color, 2, cv2.LINE_AA)

        cv2.imshow(win_name, frame)

    def __change_rot_mtx(self, filtered_detection_result, last_detection_result):
        filtered_detection_result['rotation_right_x'] = last_detection_result['rotation_right_x']
        filtered_detection_result['rotation_right_y'] = last_detection_result['rotation_right_y']
        filtered_detection_result['rotation_right_z'] = last_detection_result['rotation_right_z']
        filtered_detection_result['rotation_up_x'] = last_detection_result['rotation_up_x']
        filtered_detection_result['rotation_up_y'] = last_detection_result['rotation_up_y']
        filtered_detection_result['rotation_up_z'] = last_detection_result['rotation_up_z']
        filtered_detection_result['rotation_forward_x'] = last_detection_result['rotation_forward_x']
        filtered_detection_result['rotation_forward_y'] = last_detection_result['rotation_forward_y']
        filtered_detection_result['rotation_forward_z'] = last_detection_result['rotation_forward_z']

class DataPublishClientUDP:

    def __init__(self, server_ip, server_port, queue, filtered_queue):
        self.server_ip = server_ip
        self.__server_port = server_port
        self.__queue = queue
        self.__filtered_queue = filtered_queue

    def listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        while True:
            data = self.__queue.get()
            sock.sendto(data.encode(), (self.server_ip, self.__server_port))
            #data = self.__filtered_queue.get()
            #sock.sendto(data.encode(), (self.server_ip, self.__server_port))


class TrackingCofig:

    def __init__(self, device_number, device_parameters_dir, show_video,
                 server_ip, server_port, marker_detection_settings, translation_offset):
        self.device_number = device_number
        self.device_parameters_dir = device_parameters_dir
        self.show_video = show_video
        self.server_ip = server_ip
        self.server_port = server_port
        self.marker_detection_settings = marker_detection_settings
        self.translation_offset = translation_offset

    @classmethod
    def persisted(cls):
        if not os.path.exists('../assets/configs/'):
            os.makedirs('../assets/configs/')

        try:
            with open('../assets/configs/tracking_config_data.pkl', 'rb') as file:
                tracking_config_data = pickle.load(file)

                return cls(tracking_config_data['device_number'],
                           tracking_config_data['device_parameters_dir'],
                           tracking_config_data['show_video'],
                           tracking_config_data['server_ip'],
                           tracking_config_data['server_port'],
                           tracking_config_data['marker_detection_settings'],
                           tracking_config_data['translation_offset'])
        except FileNotFoundError:
            return cls(0, "", True, "", "", None, np.zeros(shape=(4, 4)))

    def persist(self):
        # Overwrites any existing file.
        with open('../assets/configs/tracking_config_data.pkl', 'wb+') as output:
            pickle.dump({
                'device_number': self.device_number,
                'device_parameters_dir': self.device_parameters_dir,
                'show_video': self.show_video,
                'server_ip': self.server_ip,
                'server_port': self.server_port,
                'marker_detection_settings': self.marker_detection_settings,
                'translation_offset': self.translation_offset}, output, pickle.HIGHEST_PROTOCOL)

def rotation_matrix_to_euler(R):
    
    sy = math.sqrt(R[0,0] * R[0,0] +  R[1,0] * R[1,0])
    
    singular = sy < 1e-6

    if  not singular :
        x = math.atan2(R[2,1] , R[2,2])
        y = math.atan2(-R[2,0], sy)
        z = math.atan2(R[1,0], R[0,0])
    else :
        x = math.atan2(-R[1,2], R[1,1])
        y = math.atan2(-R[2,0], sy)
        z = 0

    return np.array([x, y, z])

def euler_to_rotation_matrix(theta):
    
    R_x = np.array([[1,         0,                  0                   ],
                    [0,         math.cos(theta[0]), -math.sin(theta[0]) ],
                    [0,         math.sin(theta[0]), math.cos(theta[0])  ]])
        
    R_y = np.array([[math.cos(theta[1]),    0,      math.sin(theta[1])  ],
                    [0,                     1,      0                   ],
                    [-math.sin(theta[1]),   0,      math.cos(theta[1])  ]])
                               
    R_z = np.array([[math.cos(theta[2]),    -math.sin(theta[2]),    0],
                    [math.sin(theta[2]),    math.cos(theta[2]),     0],
                    [0,                     0,                      1]])
                    
    R = np.dot(R_z, np.dot(R_y, R_x ))

    return R

def create_kalman_filter(num_state, num_measurements, time):
    kalman_filter = cv2.KalmanFilter(num_state, num_measurements, type=cv2.CV_64FC1)

    kalman_filter.processNoiseCov = np.eye(num_state)*1e-5
    kalman_filter.measurementNoiseCov = np.eye(num_measurements)*1e-4
    kalman_filter.errorCovPost = np.eye(num_state)

    kalman_filter.transitionMatrix = np.eye(num_state)
    kalman_filter.transitionMatrix[0, 3] = time
    kalman_filter.transitionMatrix[1, 4] = time
    kalman_filter.transitionMatrix[2, 5] = time
    kalman_filter.transitionMatrix[3, 6] = time
    kalman_filter.transitionMatrix[4, 7] = time
    kalman_filter.transitionMatrix[5, 8] = time
    kalman_filter.transitionMatrix[0, 6] = 0.5 * time ** 2
    kalman_filter.transitionMatrix[1, 7] = 0.5 * time ** 2
    kalman_filter.transitionMatrix[2, 8] = 0.5 * time ** 2


    kalman_filter.measurementMatrix = np.zeros((3, 9))
    kalman_filter.measurementMatrix[0, 0] = 1
    kalman_filter.measurementMatrix[1, 1] = 1
    kalman_filter.measurementMatrix[2, 2] = 1
    return kalman_filter

def create_measurement_matrix(measurement):
    measurements = np.zeros(3)
    measurements[0] = measurement.get('translation_x')
    measurements[1] = measurement.get('translation_y')
    measurements[2] = measurement.get('translation_z')
        
    return measurements

def update_detection_result(filter, measurements, detection_result):
    filter.predict()
    filter.correct(measurements)
    
    estimated_position = filter.statePost
    detection_result['translation_x'] = float(estimated_position[0])
    detection_result['translation_y'] = float(estimated_position[1])
    detection_result['translation_z'] = float(estimated_position[2])