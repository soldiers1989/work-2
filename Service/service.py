import json
import time
import tornado.web
from tornado.options import options
import os
import inspect
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import collector
import update_dic
import logging
import traceback

tornado.options.define('port', default=8888, help='run on this port', type=int)
tornado.options.define("log_file_prefix", default='tornado_8888.log')
tornado.options.parse_command_line()


class MainHandler(tornado.web.RequestHandler):

    def get(self):
        try:
            query_text = self.get_argument('text')
            texts = json.loads(query_text)

            # 处理句子
            logging.info('====> 开始处理本次请求: ' + str(texts))
            final_result = []
            for i in range(len(texts)):
                # 清洗数据
                text = texts[i]
                if 0 < len(text) < 120:  # image_title长度一般不超过120
                    lock.acquire()
                    try:
                        final_dict = collector_service.collect(text)
                        final_result.append(final_dict)
                        logging.info(str(final_dict))
                    except Exception:
                        logging.error(traceback.format_exc())
                    lock.release()
                else:
                    continue
            final_json = json.dumps(final_result, ensure_ascii=False)
            self.set_header("Access-Control-Allow-Origin", "*")
            self.set_header("Access-Control-Allow-Headers", "x-requested-with")
            self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.write(final_json)
        except Exception:
            logging.error(traceback.format_exc())
            lock.release()
            print(traceback.format_exc())

        return

    def post(self):
        self.get()


class WatchFileThread(threading.Thread):
    """
    监测文件改变的线程
    """

    def run(self):
        event_handler = MyHandler()
        observer = Observer()
        directory = os.path.dirname(os.path.abspath(inspect.getsourcefile(lambda: 0)))
        observer.schedule(event_handler, directory, recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(5)
        except Exception as e:
            print(e)
            observer.stop()


class MyHandler(FileSystemEventHandler):

    def on_modified(self, event):
        # /dict/phrase 被修改，重新加载词典
        if event.key[0] == 'modified' and 'phrase' in event.key[1].split(r'/')[-1] and event.key[2] is False:
            logging.info('开始重新加载自定义不可切分词典')
            lock.acquire()
            collector_service.reload_dict()
            lock.release()
            logging.info('完成加载自定义不可切分词典')
        # Waring! 发现重载自定义词典会报错
        # /hanlp.properties 被修改，重新加载Hanlp分词词典
        # if event.key[0] == 'modified' and 'hanlp' in event.key[1] and event.key[2] is False:
        #     logging.info('开始重新加载 Hanlp 自定义词典')
        #     collector_service.segmentor.reload_custom_dictionry()
        #     logging.info('完成加载 Hanlp 自定义词典')


if __name__ == "__main__":
    collector_service = collector.Collector()

    lock = threading.Lock()

    t = WatchFileThread()
    t.start()

    t_d = update_dic.UpdateDictThread()
    t_d.start()

    settings = {
        'template_path': 'views',  # html文件
        'static_path': 'statics',  # 静态文件（css,js,img）
        'static_url_prefix': '/statics/',  # 静态文件前缀
        'cookie_secret': 'adm',  # cookie自定义字符串加盐
    }

    application = tornado.web.Application([(r"/", MainHandler), ], **settings)
    application.listen(options.port)
    logging.info('CRF SERVICE 已经开启！')
    tornado.ioloop.IOLoop.instance().start()
