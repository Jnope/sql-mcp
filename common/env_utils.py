import os


def remove_proxy():
    os.environ.pop('HTTP_PROXY', None)
    os.environ.pop('HTTPS_PROXY', None)
    os.environ.pop('http_proxy', None)
    os.environ.pop('https_proxy', None)
    os.environ.setdefault('NO_PROXY', "*")
    os.environ.setdefault('no_proxy', "*")
