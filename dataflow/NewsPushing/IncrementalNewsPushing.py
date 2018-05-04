import datetime
import Utils
import time
import redis
import requests
import concurrent.futures
from thrift.transport import TSocket
from thrift.protocol import TBinaryProtocol
from hbase import Hbase
from hbase.Hbase import *
from logging.handlers import RotatingFileHandler
import traceback
import hashlib
import ast
import site_rank
import hstc


"""
该脚本读取Redis队列里的每日增量的新闻数据（Redis队列里存储的是Hbase的新闻的rowkey），然后通过thrift访问Hbase，按照rowkey取出新闻的数据
并作相关字段转换后post到Solr服务上
"""


REDIS_IP = '10.174.97.43'
REDIS_PORT = 8801
REDIS_PASSWORD = "e65f63bb02d3"
REDIS_QUEUE = 'index_pending_queue'

THRIFT_IP = '10.27.68.197'
THRIFT_PORT = 9099
HBASE_TABLE_NAME = b'news_data'

# News Test: http://10.168.20.246:8080/solrweb/indexByUpdate?single=true&core_name=core_news
# News Product: http://10.27.6.161:8080/solrweb/indexByUpdate?single=true&core_name=core_news

POST_URLS = ['http://10.27.6.161:8080/solrweb/indexByUpdate?single=true&core_name=core_news']

# Thrift Client
transport = TSocket.TSocket(THRIFT_IP, THRIFT_PORT)
transport = TTransport.TBufferedTransport(transport)
protocol = TBinaryProtocol.TBinaryProtocol(transport)
client = Hbase.Client(protocol)
transport.open()


def get_hbase_row(rowkey):
    """
    通过 Thrift读取 Hbase 中 键值为 rowkey 的某一行数据，返回该行数据的 dict 表示
    :param rowkey:
    :return:
    """
    rowkey = bytes(rowkey, encoding='utf-8') if isinstance(rowkey, str) else rowkey
    row = client.getRow(HBASE_TABLE_NAME, rowkey, attributes=None)
    if len(row) > 0:
        result = dict()
        result['rowKey'] = str(row[0].row, 'utf-8')
        columns = row[0].columns
        for column in columns:
            result[str(column, 'utf-8').split(':')[-1]] = str(columns[column].value, 'utf-8')
        return result
    else:
        logger.error("未在 Hbase 中找到该条数据，请求rowKey为:" + str(rowkey, encoding='utf-8')
                     + '，错误为' + traceback.format_exc())
        return {}


def insert(table_name, row):
    """
    向表中插入一条数据
    :param table_name: 表名
    :param row: 一条数据， 格式为{'row_key':'ad97c74a38b6','cf1:field1':'data...', 'cf2:field1':'data...'}
    :return:
    """

    row_key = row['row_key'].encode('utf-8')
    mutations = []
    for item in row:
        if item != 'row_key':
            key = bytes(item, encoding="utf8")
            var = bytes(str(row[item]), encoding="utf8")
            # hbase.client.keyvalue.maxsize 默认是10M，超出这个值则设置为None
            if len(var) < 10 * 1024 * 1024:
                mutations.append(Hbase.Mutation(column=key, value=var))
            else:
                raise IllegalArgument("row_key: " + row['row_key'] + ' 的数据的 ' + item + ' 字段的值大小超过了10M ')
    client.mutateRow(table_name, row_key, mutations, {})


def post(url, rowkey, news_json, write_back_redis=True):
    """
    将单条数据 post 到 Solr 服务上
    :param url:
    :param rowkey:
    :param news_json:
    :param write_back_redis: 是否将 post 失败的数据再重新写回到 Redis
    :return:
    """
    redis_client = redis.Redis(host=REDIS_IP, port=REDIS_PORT, password=REDIS_PASSWORD)
    try:
        response = requests.post(url, json=[news_json])
        if response.status_code != 200 and write_back_redis:
            logger.error(str(redis_client.llen(REDIS_QUEUE)) + "    推送 Solr 返回响应代码 " +
                         str(response.status_code) + "，数据 rowKey:" + rowkey
                         + '，错误为' + traceback.format_exc())
            redis_client.lpush(REDIS_QUEUE, rowkey)
        else:
            logger.info(str(redis_client.llen(REDIS_QUEUE)) + "    推送 Solr 完成， rowKey:" + rowkey)
    except Exception as e:
        logger.error(str(redis_client.llen(REDIS_QUEUE)) + "    推送 Solr 异常： " + str(e) + "，数据 rowKey:" + rowkey
                     + '，错误为' + traceback.format_exc())
        if write_back_redis:
            redis_client.lpush(REDIS_QUEUE, rowkey)


def send(x, hs):
    """
    将数据作对应字段转换后 post 到 Solr 服务
    :param x:
    :return:
    """

    site_ranks = site_rank.site_ranks

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:

        for row in x:
            if row is None or not isinstance(row, dict) or row == {}:
                return
            news_json = dict({
                "id": "",
                "author": "",  # author
                "category": "",  # 新闻类型，比如'全球'，'行业'，'股票'
                "channel": "",  # 首页 新闻中心 新闻
                "contain_image": "",  # False
                "content": "",
                "crawl_time": "",  # 2017-12-27 16:01:23
                "brief": "",  # dese
                "doc_feature": "",  # 区分文档的特征，现为 title 的 md5 值
                "first_image_oss": "",  # 资讯的第一个图片oss链接
                "source_url": "",  # laiyuan
                "publish_time": "",  # 2017-12-01 10:20:49
                "source_name": "",  # source
                "title": "",  # title
                "url": "",  # url
                "tags": "",
                'doc_score': 1.0,  # 网站 page rank 分值
                "time": 0,
                "keywords": ''
            })

            news_json['id'] = row['rowKey']
            news_json['author'] = row['author'] if 'author' in row else ''

            news_json['author'] = Utils.author_norm(row['author']) \
                if row['author'] is not None else row['author']
            news_json['category'] = row['category'] if 'category' in row else '其他'
            news_json['channel'] = row['channel'] if 'channel' in row else ''
            news_json['contain_image'] = row['contain_image'] if 'contain_image' in row else False

            news_json['content'] = Utils.content_norm(row['content']) \
                if row['content'] is not None else row['content']

            news_json['crawl_time'] = row['crawl_time'] if 'crawl_time' in row else ''
            news_json['brief'] = row['dese'] if 'dese' in row else ''

            if 'title' in row and row['title'] != '' and row['title'] is not None and \
                    'content' in row and row['content'] != '' and row['content'] is not None:
                try:
                    r = hs.get_hash(row['title'], news_json['content'])
                    news_json['doc_feature'] = r[0]
                    news_json['keywords'] = ' '.join(r[1])
                    # 计算好的结果写入到 Hbase
                    insert('news_data',
                           {'row_key': row['rowKey'],
                            'info:doc_feature': news_json['doc_feature'],
                            'info:keywords': news_json['keywords']})
                except Exception:
                    logger.error(traceback.format_exc())

            if 'image_list' in row and row['image_list'] != '' and row['image_list'] != '[]':
                try:
                    image_list = ast.literal_eval(row['image_list'])
                    if isinstance(image_list, list):
                        news_json['first_image_oss'] = image_list[0]
                except Exception:
                    logger.error(traceback.format_exc())

            news_json['source_url'] = row['laiyuan'] if 'laiyuan' in row else ''
            news_json['source_name'] = row['source'] if 'source' in row else ''
            news_json['title'] = row['title'] if 'title' in row else ''
            news_json['url'] = row['url'] if 'url' in row else ''
            news_json['tags'] = row['tag'] if 'tag' in row else ''

            domain = news_json['url'].replace('https://', '').replace('http://', '').replace('www.', '').split('/')
            if domain[0] in site_ranks:
                news_json['doc_score'] = site_ranks[domain[0]] if site_ranks[domain[0]] != 0.0 else 1.0

            try:
                # 时间早于 2000 年的不推送
                if row['publish_time'][0:4] < '2000':
                    continue
                news_json['time'] = int(datetime.datetime.strptime(row['publish_time'], '%Y-%m-%d %H:%M:%S')
                                        .strftime('%s'))
                news_json['publish_time'] = row['publish_time']
            except:
                try:
                    t = Utils.time_norm(news_json['publish_time'])
                    news_json['time'] = int(datetime.datetime.strptime(t, '%Y-%m-%d %H:%M:%S').strftime('%s'))
                    news_json['publish_time'] = t
                except:
                    continue
                    # news_json['publish_time'] = str(datetime.datetime.utcfromtimestamp(0))
                    # news_json['time'] = 0

            for url in POST_URLS:
                executor.submit(post, url, row['rowKey'], news_json)


if __name__ == '__main__':

    # 记载消息推送的 logger
    handle = RotatingFileHandler('./news_pushing.log', maxBytes=5 * 1024 * 1024, backupCount=1)
    handle.setLevel(logging.INFO)
    handle.setFormatter(
        logging.Formatter('%(asctime)s - %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s')
    )
    logger = logging.getLogger('NewsPushing')
    logger.addHandler(handle)
    # logger.setLevel(logging.INFO)

    # 记载时间解析失败例子的 logger
    time_parsing_handle = RotatingFileHandler('./time_parsing.log', maxBytes=10 * 1024 * 1024, backupCount=3)
    time_parsing_handle.setFormatter(
        logging.Formatter('%(asctime)s - %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s')
    )
    logger_time_parsing = logging.getLogger('TimeParsingLogger')
    logger_time_parsing.addHandler(time_parsing_handle)
    logger_time_parsing.setLevel(logging.INFO)

    r = redis.Redis(host=REDIS_IP, port=REDIS_PORT, password=REDIS_PASSWORD)

    count_interval = 1 * 60
    start_time = time.time()
    start_queue_size = r.llen(REDIS_QUEUE)
    queue_out_count = 0

    post_url = 'http://10.168.117.133:2999/watch'
    post_interval = 10  # 每 10s 发送一次
    post_time = time.time()

    hs = hstc.Hash()

    while True:
        rowkey = r.rpop(name=REDIS_QUEUE)

        if rowkey:
            rowkey = str(rowkey, encoding='utf-8') if isinstance(rowkey, bytes) else rowkey
            news = get_hbase_row(rowkey)
            send([news], hs)

            if time.time() - post_time > post_interval:
                try:
                    requests.post(post_url,
                                  data={
                                      'name': 'news_pushing',
                                      'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                except:
                    pass
                post_time = time.time()

            queue_out_count += 1
            if time.time() - start_time > count_interval:
                end_queue_size = r.llen(REDIS_QUEUE)
                queue_in_count = end_queue_size - start_queue_size + queue_out_count
                logger.warning(str(end_queue_size) + '    在过去的' + str(count_interval/60) + '分钟内队列里写入了 '
                               + str(queue_in_count) + ' 条，写出了 ' + str(queue_out_count) + ' 条')
                start_time = time.time()
                start_queue_size = end_queue_size
                queue_out_count = 0

        else:
            logger.warning('Redis 队列中无数据，等待5s再取')
            time.sleep(5)
