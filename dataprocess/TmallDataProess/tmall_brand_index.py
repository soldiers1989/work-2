from pyspark import SparkConf, SparkContext
from pyspark.sql import SQLContext, SparkSession
import datetime
import pymongo

# 国内 MongoDB 连接信息
MONGODB_HOST = 'dds-bp1d09d4b278ceb41.mongodb.rds.aliyuncs.com'
MONGODB_PORT = 3717
USER = 'search'
PASSWORD = 'ba3Re3ame+Wa'


def remove_duplicate_data(x):
    """
    先除去一个商品一天中较早的数据（只保留该商品该天抓取最晚的数据），如果某个商品当天数据没爬取到，则进行补齐
    :param x:
    :return:
    """
    result = []
    latest_date = ''
    for data in x:
        pid = data[0]
        info = data[1]
        shopId = None
        shopName = None
        data_set = dict()
        earliest_date = ''
        for i in info:
            status = i['status']
            if 'shopId' in i and i['shopId'] is not None:
                shopId = i['shopId']
            if 'shopName' in i and i['shopName'] is not None:
                shopName = i['shopName']
            price = i['price'] if 'price' in i else None
            priceSales = i['priceSales'] if 'priceSales' in i else None
            fetchedAt = i['fetchedAt']
            date = fetchedAt.split(' ')[0]
            earliest_date = date if date < earliest_date or earliest_date == '' else earliest_date
            latest_date = date if date > latest_date or latest_date == '' else latest_date
            if status == '0':
                data_set[date] = {'status': '0', 'fetchedAt': fetchedAt, 'price': None}
            else:
                if priceSales is None and price is None:
                    normed_price = None
                elif priceSales is not None and price is None:
                    normed_price = priceSales
                elif priceSales is None and price is not None:
                    normed_price = price
                else:
                    normed_price = price if priceSales == 0.0 else priceSales
                if shopId is None or shopName is None or normed_price is None or normed_price == 0:
                    continue
                if date not in data_set:
                    data_set[date] = {'status': '1', 'fetchedAt': fetchedAt, 'price': normed_price}
                elif date in data_set and data_set[date]['fetchedAt'] < fetchedAt:
                    data_set[date]['status'] = '1'
                    data_set[date]['fetchedAt'] = fetchedAt
                    data_set[date]['price'] = normed_price
        # 修补缺失的数据
        date_range = (datetime.datetime.strptime(latest_date, '%Y-%m-%d').date() -
                      datetime.datetime.strptime(earliest_date, '%Y-%m-%d').date()).days
        amend_step = 0  # 单个商品连续多天补齐数据的最大次数，不能超过5次
        for i in range(date_range):
            date1 = str(datetime.datetime.strptime(earliest_date, '%Y-%m-%d').date() + datetime.timedelta(days=i+1))
            date2 = str(datetime.datetime.strptime(earliest_date, '%Y-%m-%d').date() + datetime.timedelta(days=i))
            if date1 in data_set:
                amend_step = 0
            else:
                if date2 in data_set and amend_step <= 5 and data_set[date2]['status'] == '1':
                    data_set[date1] = data_set[date2]
                    amend_step = amend_step + 1
                else:
                    break
        for i in data_set:
            if data_set[i]['status'] == '1':
                result.append({'pid': pid, 'date': i, 'shopId': shopId, 'shopName': shopName, 'price': data_set[i]['price']})
    return result


def cacul_brand_index(x):
    """
    计算品牌价格指数
    :param x:
    :return:
    """
    result = []
    for data in x:
        shopId = data[0]
        info = data[1]
        shopName = ''
        data_set = dict()
        earliest_day = ''
        lastest_day = ''
        for i in info:
            shopName = i['shopName']
            date = i['date']
            pid = i['pid']
            price = i['price']
            if earliest_day == '' or earliest_day > date:
                earliest_day = date
            if lastest_day == '' or lastest_day < date:
                lastest_day = date
            if date not in data_set:
                data_set[date] = {pid: price}
            else:
                data_set[date][pid] = price
        result.append({'shopId': shopId, 'shopName': shopName, 'date': earliest_day, 'ratio': 1.0, 'index': 1.0})
        last_day_index = 1.0
        cursor_day = datetime.datetime.strptime(earliest_day, '%Y-%m-%d').date() + datetime.timedelta(days=1)
        while str(cursor_day) <= lastest_day:
            if str(cursor_day) in data_set and str(cursor_day - datetime.timedelta(days=1)) in data_set:
                last_day_data = data_set[str(cursor_day - datetime.timedelta(days=1))]
                today_data = data_set[str(cursor_day)]
                sum = 0
                num = 0
                for i in today_data:
                    if i in last_day_data:
                        # 两天间的价格突然变动可能是促销，预售，抢购等情况，这种情况要去掉，设置变动阈值为2
                        if today_data[i] / last_day_data[i] < 0.5 or today_data[i] / last_day_data[i] > 2:
                            sum += 1
                            num += 1
                        else:
                            sum += today_data[i] / last_day_data[i] if last_day_data[i] != 0 else 1
                            num += 1
                ratio = sum / num if num != 0 else 1.0
                index = last_day_index * ratio
                last_day_index = index
                result.append(
                    {'shopId': shopId, 'shopName': shopName, 'date': str(cursor_day), 'ratio': ratio, 'index': index})
            else:
                result.append(
                    {'shopId': shopId, 'shopName': shopName, 'date': str(cursor_day), 'ratio': 1.0, 'index': last_day_index})
            cursor_day += datetime.timedelta(days=1)
    return result


def write_to_mongo(x):
    """
    将计算后的结果写入MongoDB
    :param x:
    :return:
    """
    for data in x:
        client = pymongo.MongoClient(MONGODB_HOST, MONGODB_PORT)
        db = client['cr_data']
        db.authenticate(USER, PASSWORD)
        collection = db['tmall_brand_index']
        _id = data['date'] + '_' + data['stock_code']
        op = {
            'date': data['date'],
            'ratio': data['ratio'],
            'index': data['index'],
            'shopId': data['shopId'],
            'shopName': data['shopName'],
            'brand': data['brand'],
            'stock_code': data['stock_code'],
            'last_updated': datetime.datetime.now()
        }
        collection.update_one({'_id': _id}, {'$set': op}, upsert=True)


if __name__ == '__main__':
    conf = SparkConf().setAppName("Tmall_Brand_Index")
    sc = SparkContext(conf=conf)
    sqlContext = SQLContext(sc)

    sparkSession = SparkSession.builder \
        .enableHiveSupport() \
        .config(conf=conf) \
        .getOrCreate()
    sparkSession.sparkContext.setLogLevel('WARN')

    df = sparkSession.sql("SELECT pid, shopId, shopName, price, priceSales, fetchedAt, status "
                          "FROM spider_data.tmall_product_v2 "
                          "WHERE pid is not null AND fetchedAt is not null AND status is not null")

    rdd1 = df.rdd.groupBy(lambda x: x['pid']).mapPartitions(lambda x: remove_duplicate_data(x))
    rdd2 = rdd1.groupBy(lambda x: x['shopId']).mapPartitions(lambda x: cacul_brand_index(x))

    shop_index_df = rdd2.toDF()
    shop_index_df.registerTempTable('table1')
    shop_mappings_df = sparkSession.sql("SELECT * FROM abc.shop_mappings")
    shop_mappings_df.registerTempTable('table2')

    result_df = sparkSession.sql("SELECT table1.date, table1.ratio, table1.index, table1.shopId, table1.shopName, "
                                 "table2.brand, table2.stock_code "
                                 "FROM table1 JOIN table2 ON table1.shopId = table2.shopId")

    result_df.show(100)

    result_df.rdd.foreachPartition(lambda x: write_to_mongo(x))
