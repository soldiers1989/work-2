import re
import datetime
from bs4 import BeautifulSoup


def time_norm(string):
    """
    给出一个时间字符串，返回该字符串中包含的时间的归一化形式
    :param string: 输入的包含时间的字符串，目前支持的格式包括： ‘2018/01/01’ ‘2018-01-01’ ‘2018年01月01日’
    ‘12/03/2017’ ‘一月 1, 2018'’ ‘10：23 pm’ '3小时前'
    :return: 归一化的时间字符串，比如 ‘2018-01-31 18:19:53’
    """

    number_mapping_1 = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8',
                        '九': '9', '十': '10', '十一': '11', '十二': '12'}
    month_mapping_2 = {'january': '一月', 'february': '二月', 'march': '三月', 'april': '四月', 'may': '五月',
                       'june': '六月', 'july': '七月', 'august': '八月', 'september': '九月', 'october': '十月',
                       'november': '十一月', 'december': '十二月'}

    # 标准的"Y/m/d"时间表示，比如 2017/12/03， 2018年5月3日
    date_regx = '\d{4}[-年/][01]?\d[-月/][0-3]?\d日?'
    # "d/m/Y" 或 "m/d/Y" 格式的时间，解析时优先按 "d/m/Y" 解析
    date_regx_2 = '[0-3]?\d/[0-3]?\d/\d{4}?'  # 比如 12/03/2017, 27/03/2017
    # 其他的日期表述1
    date_regx_3 = '(?:一|二|三|四|五|六|七|八|九|十|十一|十二)月 [0-3]?\d, \d{4}'  # 比如 '四月 10, 2017'
    # 其他的日期表述2
    date_regx_4 = '[01]?\d[-月/][0-3]?\d日?'
    clock_regx = '[0-2]?\d:[0-5]?\d(?::[0-5]\d)?(?: PM)?'  # 比如 3:01 PM

    others_1 = '\d{1,2}(?:秒|分|分钟|小时)前'

    if not isinstance(string, str) or string == '':
        return ''

    # 先归一化当前日期
    date_match1 = re.findall(date_regx, string)
    date_match2 = re.findall(date_regx_2, string)
    new_string = ' '.join([month_mapping_2[i.lower()] if i.lower() in month_mapping_2 else i
                           for i in string.split(' ')])
    date_match3 = re.findall(date_regx_3, new_string)
    date_match4 = re.findall(date_regx_4, new_string)
    if date_match1:
        raw_date = date_match1[0]
        date = '-'.join(re.split('[-年月日/]', raw_date)).strip('-')
    elif date_match2:
        raw_date = date_match2[0]
        try:
            date = str(datetime.datetime.strptime(raw_date, '%m/%d/%Y')).split(' ')[0]
        except ValueError:
            date = str(datetime.datetime.strptime(raw_date, '%d/%m/%Y')).split(' ')[0]
    elif date_match3:
        raw_date = date_match3[0]
        raw_date = number_mapping_1[raw_date.split('月')[0]] + raw_date.split('月')[1]
        date = str(datetime.datetime.strptime(raw_date, '%m %d, %Y')).split(' ')[0]
    elif date_match4:
        raw_date = date_match4[0]
        date = '-'.join(re.split('[-年月日/]', raw_date)).strip('-')
        str_date = str(datetime.datetime.strptime(date, '%m-%d')).split(' ')[0][5:]
        current_date = str(datetime.datetime.now()).split(' ')[0]
        date = current_date.split(' ')[0][:5] + str_date if current_date.split(' ')[0][5:] >= str_date \
            else ''
    else:
        date = ''

    # 再归一化小时，分钟和秒
    clock = ''
    clock_match = re.findall(clock_regx, string, flags=re.IGNORECASE)
    if clock_match:
        raw_clock = clock_match[0]
        raw_time_segs = re.split(':', raw_clock)
        is_pm = 'PM' in raw_clock.upper() and int(raw_time_segs[0]) < 12

        if is_pm and len(raw_time_segs) == 3:
            clock = str(datetime.datetime.strptime(raw_clock, '%I:%M:%S %p')).split(' ')[1]
        elif is_pm and len(raw_time_segs) == 2:
            clock = str(datetime.datetime.strptime(raw_clock, '%I:%M %p')).split(' ')[1]
        elif not is_pm and len(raw_time_segs) == 3:
            clock = str(datetime.datetime.strptime(raw_clock.strip('[ pPmM]'), '%H:%M:%S')).split(' ')[1]
        elif not is_pm and len(raw_time_segs) == 2:
            clock = str(datetime.datetime.strptime(raw_clock.strip('[ pPmM]'), '%H:%M')).split(' ')[1]
    else:
        clock = '00:00:00'

    # 特殊的表述方式，比如'3分钟前'
    others_1_match = re.findall(others_1, string)
    current = ''
    if others_1_match:
        raw_time = others_1_match[0]
        if '秒' in raw_time:
            minute_minus = re.search('\d{1,2}', raw_time).group()
            current = str(datetime.datetime.now() - datetime.timedelta(seconds=int(minute_minus))).split('.')[0]
        if '分' in raw_time:
            minute_minus = re.search('\d{1,2}', raw_time).group()
            current = str(datetime.datetime.now() - datetime.timedelta(minutes=int(minute_minus))).split('.')[0]
        if '时' in raw_time:
            minute_minus = re.search('\d{1,2}', raw_time).group()
            current = str(datetime.datetime.now() - datetime.timedelta(hours=int(minute_minus))).split('.')[0]

    if others_1_match:
        # print(string, '<->', current)
        return current
    elif (date_match1 or date_match2 or date_match3 or date_match4) and date != '':
        # print(string, '<->', date + ' ' + clock)
        return date + ' ' + clock
    else:
        # print(string, '<->', '')
        return ''


def author_norm(string):
    additionals = ['记者', '作者', '编辑', '责任编辑', '分析师', '来源']
    return ' '.join([j for j in [i for i in re.split('[\n\r :：（）()]', string) if i != ''] if j not in additionals])


def content_norm(html):
    soup = BeautifulSoup(html, 'html.parser')
    for s in soup(['script', 'style']):
        s.decompose()

    result = ' '.join(soup.stripped_strings)
    result = re.sub('\r', ' ', result)
    result = re.sub('\t', ' ', result)
    result = re.sub('( *\n+ *)+', '\n', result)
    result = re.sub(' {2,}', ' ', result)

    return result
