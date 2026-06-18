from crawler import PostCrawler
from crawler import CommentCrawler
import threading


def post_thread(stock_symbol, start_page, end_page):  # stock_symbol为股票的代码，page为想要爬取的页面范围
    post_crawler = PostCrawler(stock_symbol)
    post_crawler.crawl_post_info(start_page, end_page)


def post_detail_thread(stock_symbol, limit=None, url_type='all'):
    """爬取帖子详情页的完整正文内容
    
    对已爬取的帖子，逐个访问详情页提取正文内容。
    默认爬取所有未爬取正文的帖子，可通过 limit 参数限制数量。
    
    Args:
        stock_symbol: 股票代码
        limit: 限制爬取的帖子数量，None 表示爬取所有
        url_type: 筛选帖子类型，'all' 表示所有，'caifuhao' 表示仅财富号，'guba' 表示仅股吧原生
    """
    post_crawler = PostCrawler(stock_symbol)
    post_crawler.crawl_post_detail(limit, url_type)


def comment_thread_date(stock_symbol, start_date, end_date):  # stock_symbol为股票的代码，date为想要爬取的日期范围
    comment_crawler = CommentCrawler(stock_symbol)
    comment_crawler.find_by_date(start_date, end_date)
    comment_crawler.crawl_comment_info()


def comment_thread_id(stock_symbol, start_id, end_id):  # stock_symbol为股票的代码，id是通过post_id来确定爬取，适合断联续爬
    comment_crawler = CommentCrawler(stock_symbol)
    comment_crawler.find_by_id(start_id, end_id)
    comment_crawler.crawl_comment_info()


if __name__ == "__main__":

    # 爬取发帖信息（第 1-50 页）
    #thread1 = threading.Thread(target=post_thread, args=('601138', 1, 50))  # 设置想要爬取的股票代码，开始与终止页数
    #thread2 = threading.Thread(target=post_thread, args=('000630', 1, 50))  # 可同时进行多个线程

    # 爬取帖子正文内容（需先运行 post_thread 获取帖子列表）
    # limit 参数限制爬取数量，None 表示爬取所有帖子
    # url_type 参数筛选帖子类型：'all' 所有, 'caifuhao' 仅财富号, 'guba' 仅股吧原生
    #thread1 = threading.Thread(target=post_detail_thread, args=('601138', None, 'caifuhao'))

    # 爬取评论信息，注意需先爬取发帖信息储存到数据库里后才可以爬取评论信息（因为需要用到第一步中的url）
    thread1 = threading.Thread(target=comment_thread_date, args=('601138', '2020-01-01', '2026-06-08'))
    # thread2 = threading.Thread(target=comment_thread_date, args=('000729', '2020-01-01', '2023-12-31'))

    # 中断之后重新通过_id接着爬取
    # thread1 = threading.Thread(target=comment_thread_id, args=('000651', 384942, 411959))
    # thread2 = threading.Thread(target=comment_thread_id, args=('000651', 62929, 321047))

    thread1.start()
    # thread2.start()

    thread1.join()
    # thread2.join()

    print(f"you have fetched data successfully, congratulations!")
