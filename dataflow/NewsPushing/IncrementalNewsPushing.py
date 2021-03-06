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
import re
from apscheduler.schedulers.background import BackgroundScheduler
import os
import json
import pymysql
from ac_search import ACSearch
import site_rank
import hstc
import wechat_subscription


"""
该脚本读取Redis队列里的每日增量的新闻数据（Redis队列里存储的是Hbase的新闻的rowkey），然后通过thrift访问Hbase，按照rowkey取出新闻的数据
并作相关字段转换后post到Solr服务上
"""


REDIS_IP = '10.174.97.43'
REDIS_PORT = 8801
REDIS_PASSWORD = "e65f63bb02d3"
REDIS_QUEUE = 'index_pending_queue'

# 原来的资讯的推送地址 'http://10.165.101.72:8086/news_update'
# 新加的资讯的推送地址 'http://10.80.62.207:8080/onlySolr/core_news'
POST_URLS = ['http://10.165.101.72:8086/news_update', 'http://10.80.62.207:8080/onlySolr/core_news/update?wt=json']

# 推送资讯的用于近期title去重的Redis连接信息，不同的消息推送的存储title的 Sorted Set 的 setname 不一样，保持
# POST_URLS与 DereplicationRedis 的一致性
DereplicationRedis = [
    {
        'ip': '10.81.88.218',
        'port': 8103,
        'password': 'qQKQwjcB0bdqD',
        'setname': "latest_titles"
    },
    {
        'ip': '10.81.88.218',
        'port': 8103,
        'password': 'qQKQwjcB0bdqD',
        'setname': "solr2_latest_titles"
    }
]

THRIFT_IP = '10.27.68.197'
THRIFT_PORT = 9099
HBASE_TABLE_NAME = b'news_data'


# Thrift Client
transport = TSocket.TSocket(THRIFT_IP, THRIFT_PORT)
transport = TTransport.TBufferedTransport(transport)
protocol = TBinaryProtocol.TBinaryProtocol(transport)
client = Hbase.Client(protocol)
transport.open()


class StockInformer:

    def __init__(self):
        self.stock_info_file = './stock_info_file.txt'
        self.stock_info = {}

        # 读取本地的股票行业信息
        def load_file():
            with open(self.stock_info_file) as f:
                for line in f:
                    infos = line.strip('\n').split('\t')
                    stock_code = infos[0]
                    stk_code = infos[1]
                    stock_name = infos[2]
                    stock_industry = infos[3]

                    # 美股的股票代码是英文缩写，不匹配股票代码，只匹配股票名称，因为英文缩写匹配太广泛，比如美股的 “A” 表示 “安捷伦科技”
                    if stk_code.endswith(".N") or stk_code.endswith(".O") or stk_code.endswith(".A"):
                        self.stock_info[stock_name] = (stock_code, stk_code, stock_name, stock_industry)
                    else:
                        self.stock_info[stock_name] = (stock_code, stk_code, stock_name, stock_industry)
                        self.stock_info[stock_code] = (stock_code, stk_code, stock_name, stock_industry)

        if os.path.exists(self.stock_info_file):
            load_file()
        else:
            self.update()
            load_file()

        self.ac = ACSearch()
        for i in self.stock_info:
            self.ac.add_word(i)
        self.ac.start()

    # 从线上MySQL数据库拉取股票代码，名称和行业等信息
    def update(self):

        host = '10.117.211.16'
        port = 6033
        user = 'stin_sys_ro_pe'
        password = 'b405038da87d'
        db = 'r_reportor'
        stock_info = {}
        try:
            connection = pymysql.connect(host=host, port=port, db=db,
                                         user=user, password=password, charset='utf8',
                                         cursorclass=pymysql.cursors.DictCursor)

            # 选取美股（GICS行业标准），港股（GICS行业标准），A股（申银行业标准）
            sql = "SELECT sec_basic_info.sec_code, sec_industry_new.stk_code, sec_basic_info.sec_name, " \
                  "sec_industry_new.second_indu_name FROM r_reportor.sec_basic_info join r_reportor.sec_industry_new " \
                  "WHERE sec_basic_info.sec_uni_code = sec_industry_new.sec_uni_code AND " \
                  "(indu_standard = '1001007' OR indu_standard = '1001016') AND sec_industry_new.if_performed = '1';"
            cursor = connection.cursor()
            cursor.execute(sql)
            text = ''
            for row in cursor:
                stock_code = row['sec_code']
                stk_code = row['stk_code']
                stock_name = row['sec_name']
                stock_industry = row['second_indu_name']
                text += stock_code + '\t' + stk_code + '\t' + stock_name + '\t' + stock_industry + '\n'

            with open(self.stock_info_file, 'w') as f:
                f.write(text)
        except Exception as e:
            raise e

    def extract_stock_info(self, text):
        matched_list = self.ac.search(text)
        matched_list = list(set(matched_list))
        result = {'stock_code': [], 'stock_name': [], 'stock_industry': []}
        for item in matched_list:
            if re.match('\d{4,}', item):  # 匹配到股票代码
                if self.stock_info[item][1] in result['stock_code']:
                    continue
                result['stock_name'].append(self.stock_info[item][2])
                result['stock_code'].append(self.stock_info[item][1])
                result['stock_industry'].append(self.stock_info[item][3])
            else:  # 匹配到股票名字
                if self.stock_info[item][2] in result['stock_name']:
                    continue
                result['stock_name'].append(self.stock_info[item][2])
                result['stock_code'].append(self.stock_info[item][1])
                result['stock_industry'].append(self.stock_info[item][3])
        return result


def get_hbase_row(rowkey):
    """
    通过 Thrift读取 Hbase 中 键值为 rowkey 的某一行数据，返回该行数据的 dict 表示
    :param rowkey:
    :return:
    """
    try:
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
    except Exception as e:
        logger.error(e)


def insert(table_name, row):
    """
    向表中插入一条数据
    :param table_name: 表名
    :param row: 一条数据， 格式为{'row_key':'ad97c74a38b6','cf1:field1':'data...', 'cf2:field1':'data...'}
    :return:
    """

    row_key = bytes(row['row_key'], encoding='utf-8') if isinstance(row['row_key'], str) else row['row_key']
    table_name = bytes(table_name, encoding='utf-8') if isinstance(table_name, str) else table_name
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
    head = {'Content-Type': 'application/json'}
    params = {"overwrite": "true", "commitWithin": 100000}
    try:
        response = requests.post(url, params=params, headers=head, json=[news_json])
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


def send(x, hs, si):
    """
    将数据作对应字段转换后 post 到 Solr 服务
    :param x: 资讯数据
    :param hs: hstc.Hash() 实例
    :param si: StockInformer 实例
    :return:
    """
    site_ranks = site_rank.site_ranks

    # 公众号
    wechat_subscriptions = wechat_subscription.wechat_subscriptions
    wechat_sub = {}
    for i in wechat_subscriptions:
        sub_id = i[0]
        name = i[1]
        category = i[2]
        is_high_quality = i[3]
        if name in wechat_sub:
            wechat_sub[name][0] = sub_id
            wechat_sub[name][1] = is_high_quality
            wechat_sub[name][2].append(category)
        else:
            wechat_sub[name] = [sub_id, is_high_quality, [category]]

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
                "doc_score": 1.0,  # 网站 page rank 分值
                "time": 0,
                "keywords": "",
                "stockcode": "",
                "stockname": "",
                "industryname": "",
                "is_high_quality": "",  # 如果来源是高质量公众号，置1，否则置0
                "category_other": ""  # 如果来源是公众号，这个字段指公众号的多个可能的类别，比如“买方”，“个人”，“媒体”等等
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
                    r = hs.get_hash(row['title'], row['content'])
                    news_json['doc_feature'] = r[0]
                    news_json['keywords'] = ' '.join(r[1])
                    # 计算好的结果写入到 Hbase
                    insert('news_data',
                           {'row_key': row['rowKey'],
                            'info:doc_feature': news_json['doc_feature'],
                            'info:keywords': news_json['keywords']})
                except Exception:
                    logger.error(traceback.format_exc())
            del news_json['keywords']  # keywords 字段先不推

            if 'image_list' in row and row['image_list'] != '' and row['image_list'] != '[]':
                try:
                    image_list = ast.literal_eval(row['image_list'])
                    if isinstance(image_list, list):
                        news_json['first_image_oss'] = image_list[0].replace("-internal.aliyuncs.com", ".aliyuncs.com")
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

            if news_json['title'] is None or news_json['title'] == "":
                continue

            # 从 title 提取出股票相关信息
            stock_info = si.extract_stock_info(news_json['title'])
            stock_pair = []
            for i in range(len(stock_info['stock_code'])):
                stock_pair.append([stock_info['stock_code'][i], stock_info['stock_name'][i]])
            news_json['stockcode'] = json.dumps(stock_pair) if stock_pair != [] else ''
            news_json['stockname'] = ','.join(stock_info['stock_name'])
            news_json['industryname'] = ','.join(stock_info['stock_industry'])

            # 当 Title 太长，截取 title
            if news_json['title'] is not None and len(news_json['title']) > 50:
                news_json['title'] = re.split('[;；?？.。\n]', news_json['title'])[0]
                # 如果句号分号问号换行仍无法切割到50以下，则尝试用逗号空格
                if len(news_json['title']) > 50:
                    news_json['title'] = re.split('[,， ]', news_json['title'])[0]
                    if len(news_json['title']) > 50:
                        news_json['title'] = news_json['title'][:50]

            # 处理公众号信息
            if news_json["source_name"] in wechat_sub:
                news_json['is_high_quality'] = wechat_sub[news_json["source_name"]][1]
                news_json['category_other'] = wechat_sub[news_json["source_name"]][2]

            for i in range(len(POST_URLS)):
                dr = DereplicationRedis[i]
                # 根据 Redis 中 Title 的缓存去重，选择是否进行推送
                dp_redis = redis.Redis(host=dr['ip'], port=dr['port'], password=dr['password'])
                normed_title = "".join(re.findall("[0-9a-zA-Z\u4e00-\u9fa5]+", news_json['title']))
                title_hash = hashlib.md5(bytes(normed_title, 'utf-8')).hexdigest()
                if dp_redis.zscore(dr['setname'], title_hash):
                    dp_redis.zadd(dr['setname'], title_hash, news_json['time'])
                else:
                    dp_redis.zadd(dr['setname'], title_hash, news_json['time'])
                    news_json['index_time'] = datetime.datetime.now().isoformat()
                    executor.submit(post, POST_URLS[i], row['rowKey'], news_json)


def redis_clean_cache(expire_hours=24*30*2):
    """
    删除 Redis 的 title 缓存中超时的数据
    :param expire_hours: 超时小时数，默认是超过两个月认为超时
    :return:
    """
    ttl = time.time() - expire_hours*60*60
    for i in DereplicationRedis:
        dp_redis = redis.Redis(host=i['ip'], port=i['port'], password=i['password'])
        num = dp_redis.zremrangebyscore(i['setname'], 0, ttl)
        logger.warning('清理完 Redis ' + i['setname'] + ' 缓存，删除 ' + str(num) + ' 条过期数据')


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

    # 定时间隔任务调度，每隔固定周期清理 title 缓存 redis 中超时的数据
    derepl_sche = BackgroundScheduler()
    derepl_sche.add_job(redis_clean_cache, 'cron', hour=2)
    derepl_sche.start()

    hs = hstc.Hash()
    si = StockInformer()

    while True:
        rowkey = r.rpop(name=REDIS_QUEUE)

        if rowkey:
            rowkey = str(rowkey, encoding='utf-8') if isinstance(rowkey, bytes) else rowkey
            news = get_hbase_row(rowkey)
            send([news], hs, si)

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
