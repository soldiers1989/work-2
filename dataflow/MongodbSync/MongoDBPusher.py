import pymongo
import redis
import threading
import time
import logging
from logging.handlers import RotatingFileHandler


# MongoDB 信息
MONGODB_HOST = 'dds-j6cd3f25db6afa741.mongodb.rds.aliyuncs.com'
MONGODB_PORT = 3717
USER = 'hk_sync'
PASSWORD = '9c9df8aebf04'

# Redis 信息
REDIS_IP = '47.97.27.84'
REDIS_PORT = 6379
OPLOG_QUEUE = 'oplog_queue'


class MongoDBPusher(threading.Thread):

    def __init__(self):
        super(MongoDBPusher, self).__init__()

        # 记载 MongoDBPusher 线程情况的 logger
        handle = RotatingFileHandler('./MongoDBPusher.log', maxBytes=5 * 1024 * 1024, backupCount=5)
        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(handle)

        self.client = pymongo.MongoClient(MONGODB_HOST, MONGODB_PORT)

    def run(self):
        while True:
            r = redis.Redis(host=REDIS_IP, port=REDIS_PORT)
            oplog_data = r.lpop(name=OPLOG_QUEUE)

            action_type = oplog_data['op']
            db = oplog_data['ns'].split(".")[0]
            table_name = oplog_data['ns'].split(".")[-1]

            database = self.client[db]
            database.authenticate(USER, PASSWORD)
            collection = database[table_name]

            if oplog_data:
                # 同步 Primary 结点的更新
                try:
                    if action_type == 'i':
                        collection.insert_one(oplog_data)
                        self.logger.info('Insert to HK MongoDB: ' + oplog_data['o2'])
                    elif action_type == 'u':
                        collection.update_one(oplog_data['o2'], oplog_data)
                        self.logger.info('Update to HK MongoDB: ' + oplog_data['o2'])
                    elif action_type == 'd':
                        collection.delete_one(oplog_data['o2'])
                        self.logger.info('Delete to HK MongoDB: ' + oplog_data['o2'])
                except Exception as e:
                    r.rpush(oplog_data)
                    self.logger.error('操作 HK MongoDB 失败，重新加到 Redis 队列末尾。 oplog 为: '
                                      + oplog_data + '错误为: ' + str(e))
            else:
                # self.logger.info('Redis oplog 队列中无数据，等待1s再取')
                time.sleep(1)