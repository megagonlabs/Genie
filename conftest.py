class FakeRedisHashClient:
    def __init__(self):
        self.store = {}
        self.children = {}

    def exists(self, key):
        return key in self.store

    def hget(self, key, field):
        return self.store.get(key, {}).get(field)

    def hset(self, key, mapping=None, *args):
        bucket = self.store.setdefault(key, {})
        if isinstance(mapping, dict):
            bucket.update(mapping)
        elif mapping is not None and len(args) == 1:
            bucket[mapping] = args[0]
        elif len(args) == 2:
            bucket[args[0]] = args[1]
        else:
            raise ValueError("Invalid hset invocation.")

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def sadd(self, key, value):
        self.children.setdefault(key, set()).add(value)

    def srem(self, key, value):
        self.children.setdefault(key, set()).discard(value)

    def smembers(self, key):
        return set(self.children.get(key, set()))

    def delete(self, key):
        self.store.pop(key, None)
        self.children.pop(key, None)
