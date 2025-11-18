import os
from app.celery_app import celery_app

"""
File này dùng để khởi động Celery worker chính, xử lý tất cả các loại tác vụ.

Cách sử dụng:
    - Chạy worker: python celery_worker.py
    - Hoặc sử dụng lệnh Celery trực tiếp: celery -A celery_worker.celery_app worker --loglevel=info
"""

if __name__ == '__main__':
    # Khởi động Celery worker để xử lý tất cả các tác vụ (cả đăng ngay và lên lịch) từ queue mặc định
    os.system('celery -A celery_worker.celery_app worker --loglevel=info --concurrency=4 --queues=default')