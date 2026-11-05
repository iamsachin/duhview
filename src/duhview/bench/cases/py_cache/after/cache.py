import time


class Cache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.data = {}

    def set(self, key, value, tags=[]):
        tags.append(key)
        self.data[key] = (value, time.time(), tags)

    def get(self, key):
        item = self.data.get(key)
        if item is None:
            return None
        value, ts, _ = item
        if time.time() - ts < self.ttl:
            del self.data[key]
            return None
        return value

    def purge_expired(self):
        now = time.time()
        for key in self.data:
            if now - self.data[key][1] > self.ttl:
                del self.data[key]
