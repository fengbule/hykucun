class Flask:
    def __init__(self, name):
        self.name = name
        self.secret_key = None
    def route(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator
    def post(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator
    def before_request(self, func):
        return func


def flash(*args, **kwargs):
    return None


def redirect(value):
    return value


def render_template(*args, **kwargs):
    return {"args": args, "kwargs": kwargs}


class _Request:
    endpoint = None
    method = "GET"
    def __init__(self):
        self.form = {}
    def args(self):
        return {}

request = _Request()
session = {}

def url_for(name, **kwargs):
    return f"/{name}"
