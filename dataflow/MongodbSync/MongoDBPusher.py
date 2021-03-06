import pymongo
from pymongo.errors import DuplicateKeyError
import redis
import threading
import time
import json
from bson import json_util
from bson import *
import logging
from logging.handlers import RotatingFileHandler
import traceback
import re


"""
从国内的 bj-mogo-sync002 上的 Mongo 消息队列中取出 Mongo 操作历史记录，然后将这些操作更新到香港的 Mongo 集群
"""


# 香港 MongoDB 连接信息信息
MONGODB_HOST = 'dds-j6cd3f25db6afa741.mongodb.rds.aliyuncs.com'
MONGODB_PORT = 3717
USER = 'hk_sync'
PASSWORD = '9c9df8aebf04'

# Redis 信息
REDIS_IP = '47.97.27.84'
REDIS_PORT = 6379
OPLOG_QUEUE = 'oplog'


class MongoDBPusher(threading.Thread):

    def __init__(self):
        super(MongoDBPusher, self).__init__()

        # 记载 MongoDBPusher 线程情况的 logger
        handle = RotatingFileHandler('./mongodb_pusher.log', maxBytes=50 * 1024 * 1024, backupCount=3)
        handle.setFormatter(logging.Formatter(
            '%(asctime)s %(name)-12s %(thread)d %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s'))

        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(handle)
        # self.logger.setLevel(logging.INFO)

        self.client = pymongo.MongoClient(MONGODB_HOST, MONGODB_PORT)

    def run(self):
        r = redis.Redis(host=REDIS_IP, port=REDIS_PORT)

        while True:
            oplog_data = r.lpop(name=OPLOG_QUEUE)

            if oplog_data:

                try:

                    oplog_data = str(oplog_data, 'utf-8') if isinstance(oplog_data, bytes) else oplog_data
                    try:
                        oplog_data = json_util.loads(oplog_data, object_hook=json_util.object_hook)
                    except json.decoder.JSONDecodeError:
                        try:
                            # oplog_data 不是一个json，而是一个 str(dict)
                            oplog_data = re.sub(', tzinfo=<bson.tz_util.FixedOffset object at [\da-z]*>', '', oplog_data)
                            oplog_data = eval(oplog_data)
                        except:
                            self.logger.error(traceback.format_exc())
                            continue
                    except:
                        self.logger.error(traceback.format_exc())
                        continue

                    # update 操作要使用 $set 操作符
                    if oplog_data['op'] == 'u' and '$set' not in oplog_data['o']:
                        oplog_data['o'] = {'$set': oplog_data['o']}

                    # update 的 $set 操作里不能有 '_id'，该字段在 mongo 里是唯一的
                    if oplog_data['op'] == 'u' and '_id' in oplog_data['o']['$set']:
                        del oplog_data['o']['$set']['_id']

                    action_type = oplog_data['op']
                    db = oplog_data['ns'].split(".")[0]
                    table_name = oplog_data['ns'].split(".")[-1]
                    database = self.client[db]
                    database.authenticate(USER, PASSWORD)
                    collection = database[table_name]

                    _id = oplog_data['o']['_id'] if '_id' in oplog_data['o'] else oplog_data['o2']['_id']

                    if action_type == 'i':
                        try:
                            collection.insert_one(oplog_data['o'])
                            self.logger.info(str(r.llen(OPLOG_QUEUE)) + '    Insert to HK MongoDB: ' + str(_id))
                        except DuplicateKeyError:
                            collection.replace_one({'_id': oplog_data['o']['_id']}, oplog_data['o'], True)
                            self.logger.info(str(r.llen(OPLOG_QUEUE)) + '    Insert to HK MongoDB: ' + str(_id))
                    elif action_type == 'u':
                        collection.update_one(oplog_data['o2'], oplog_data['o'], upsert=True)
                        self.logger.info(str(r.llen(OPLOG_QUEUE)) + '    Update to HK MongoDB: ' + str(_id))
                    elif action_type == 'd':
                        collection.delete_one(oplog_data['o'])
                        self.logger.info(str(r.llen(OPLOG_QUEUE)) + '    Delete to HK MongoDB: ' + str(_id))
                except Exception as e:
                    r.rpush(OPLOG_QUEUE, oplog_data)
                    self.logger.error(str(r.llen(OPLOG_QUEUE)) + '    操作 HK MongoDB 失败，重新加到 Redis 队列末尾。 '
                                      'oplog 为: ' + str(type(oplog_data)) + ' ' + str(oplog_data) + '错误为: \n'
                                      + traceback.format_exc())
                    time.sleep(0.001)
            else:
                self.logger.info('Redis oplog 队列中无数据，等待10s再取')
                time.sleep(10)
