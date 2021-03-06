import redis
import threading
import time
import oss2
import logging
from logging.handlers import RotatingFileHandler
import traceback


"""
从国内的 bj-mogo-sync002 上的 OSS 消息队列中取出 OSS 链接，如果这些香港的 OSS 服务上没有该 OSS文件，
则从国内 OSS 服务下载该文件并写到国外 OSS 服务
"""


# Redis 信息
REDIS_IP = '47.97.27.84'
REDIS_PORT = 6379
OSS_QUEUE = 'oss'

# OSS 信息
ACCESS_KEY_ID = 'LTAIUckqp8PIWkm9'
ACCESS_KEY_SECRET = 'jCsTUNa9l9zloXzdW6xvksFpDaMwY1'
BUCKET_HZ = 'abc-crawler'  # 杭州的 Bucket
BUCKET_HK = 'hk-crawler'  # 香港的 Bucket
ENDPOINT_HZ = 'oss-cn-hangzhou.aliyuncs.com'  # 连接国内的必须使用公网 Endpoint
ENDPOINT_HK = 'oss-cn-hongkong-internal.aliyuncs.com'  # 连接香港的使用内网 Endpoint 以节省流量


class OSSPusher(threading.Thread):

    def __init__(self):
        super(OSSPusher, self).__init__()

        # 记载 OSSPusher 线程情况的 logger
        handle = RotatingFileHandler('./oss_pusher.log', maxBytes=50 * 1024 * 1024, backupCount=3)
        handle.setFormatter(logging.Formatter(
            '%(asctime)s %(name)-12s %(thread)d %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s'))

        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(handle)
        # self.logger.setLevel(logging.INFO)

        self.bucket_hz = oss2.Bucket(oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET), ENDPOINT_HZ, BUCKET_HZ)
        self.bucket_hk = oss2.Bucket(oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET), ENDPOINT_HK, BUCKET_HK)

    def run(self):
        r = redis.Redis(host=REDIS_IP, port=REDIS_PORT)

        while True:

            oss_data = r.spop(name=OSS_QUEUE)

            if oss_data:
                oss_data = oss_data if isinstance(oss_data, str) else str(oss_data, encoding='utf-8')
                file_name = oss_data.split('aliyuncs.com/')[-1]
                try:
                    if not self.bucket_hk.object_exists(file_name) and self.bucket_hz.object_exists(file_name):
                        # print('开始下载', oss_data)
                        file_stream = self.bucket_hz.get_object(file_name)
                        # print('开始上传', oss_new)
                        self.bucket_hk.put_object(file_name, file_stream)
                        # self.logger.info(str(r.scard(OSS_QUEUE)) + '    转写 oss 成功，oss 为: ' + oss_new)
                except oss2.exceptions.RequestError:
                    self.logger.info(str(r.scard(OSS_QUEUE)) + '    转写 oss 失败，错误为: \n' + traceback.format_exc())
                    r.sadd(OSS_QUEUE, oss_data)
                    time.sleep(0.001)
                except Exception:
                    self.logger.error(str(r.scard(OSS_QUEUE)) + '    转写 oss 失败，错误为: \n'
                                      + traceback.format_exc())
                    r.sadd(OSS_QUEUE, oss_data)
                    time.sleep(0.001)
            else:
                self.logger.info('Redis oss 队列中无数据，等待10s再取')
                time.sleep(10)
