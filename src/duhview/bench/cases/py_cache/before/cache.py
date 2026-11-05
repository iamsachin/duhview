import time


class Cache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.data = {}

    def set(self, key, value):
        self.data[key] = (value, time.time())

    def get(self, key):
        item = self.data.get(key)
        if item is None:
            return None
        value, ts = item
        if time.time() - ts > self.ttl:
            del self.data[key]
            return None
        return value
