"""A full cache system written on top of Django's rudimentary one."""

from django.conf import settings
from django.core.cache import cache
from django.utils.encoding import smart_str
import cPickle as pickle
import md5
import types
import logging
from satchmo.shop.utils import is_string_like, is_list_or_tuple

log = logging.getLogger('caching')

CACHED_KEYS = {}
CACHE_CALLS = 0
CACHE_HITS = 0
KEY_DELIM = "::"

class CacheWrapper(object):
    def __init__(self, val, inprocess=False):
        self.val = val
        self.inprocess = inprocess

    def __str__(self):
        return str(self.val)

    def __repr__(self):
        return repr(self.val)

    @classmethod
    def wrap(cls, obj):
        if isinstance(obj, cls):
            return obj
        else:
            return cls(obj)


class MethodNotFinishedError(Exception): 
    def __init__(self, f):
        self.func = f


class NotCachedError(Exception):    
    def __init__(self, k):
        self.key = k

class CacheNotRespondingError(Exception):    
    pass
    
def cache_delete(*keys, **kwargs):
    global CACHED_KEYS
    log.debug('cache_delete')
    children = kwargs.pop('children',False)
    removed = []

    if (keys or kwargs):
        key = cache_key(*keys, **kwargs)
    
        if CACHED_KEYS.has_key(key):
            del CACHED_KEYS[key]
            removed.append(key)

        cache.delete(key)

        if children:
            key = key + KEY_DELIM
            children = [x for x in CACHED_KEYS.keys() if x.startswith(key)]
            for k in children:
                del CACHED_KEYS[k]
                cache.delete(k)
                removed.append(k)
    else:
        key = "All Keys"
        deleteneeded = _cache_flush_all()
        
        removed = CACHED_KEYS.keys()

        if deleteneeded:
            for k in CACHED_KEYS:
                cache.delete(k)
            
        CACHED_KEYS = {}

    if removed:
        log.debug("Cache delete: %s", removed)
    else:
        log.debug("No cached objects to delete for %s", key)

    return removed


def cache_delete_function(func):
    return cache_delete(['func', func.__name__, func.__module__], children=True)


def _cache_flush_all():
    if is_memcached_backend():
        cache._cache.flush_all()
        return False
    return True

def cache_function(length=settings.CACHE_TIMEOUT):
    """
    A variant of the snippet posted by Jeff Wheeler at
    http://www.djangosnippets.org/snippets/109/

    Caches a function, using the function and its arguments as the key, and the return
    value as the value saved. It passes all arguments on to the function, as
    it should.

    The decorator itself takes a length argument, which is the number of
    seconds the cache will keep the result around.

    It will put a temp value in the cache while the function is
    processing. This should not matter in most cases, but if the app is using
    threads, you won't be able to get the previous value, and will need to
    wait until the function finishes. If this is not desired behavior, you can
    remove the first two lines after the ``else``.
    """
    def decorator(func):
        def inner_func(*args, **kwargs):            
            try:
                value = cache_get('func', func.__name__, func.__module__, args, kwargs)

            except NotCachedError, e:
                # This will set a temporary value while ``func`` is being
                # processed. When using threads, this is vital, as otherwise
                # the function can be called several times before it finishes
                # and is put into the cache.

                funcwrapper = CacheWrapper(".".join([func.__module__, func.__name__]), inprocess=True)
                cache_set(e.key, value=funcwrapper, length=length, skiplog=True)
                value = func(*args, **kwargs)
                cache_set(e.key, value=value, length=length)

            return value
        return inner_func
    return decorator


def cache_get(*keys, **kwargs):
    global CACHE_CALLS, CACHE_HITS
    CACHE_CALLS += 1
    if CACHE_CALLS == 1:
        cache_require()
        
    if kwargs.has_key('default'):
        default_value = kwargs.pop('default')
        use_default = True
    else:
        use_default = False
    
    key = cache_key(keys, **kwargs)
    #log.debug("getting: %s", key)
    obj = cache.get(key)
    if obj and isinstance(obj, CacheWrapper):
        CACHE_HITS += 1
        CACHED_KEYS[key] = True
        log.debug('got cached [%i/%i]: %s', CACHE_CALLS, CACHE_HITS, key)
        if obj.inprocess:
            raise MethodNotFinishedError(obj.val)
            
        return obj.val
    else:
        if CACHED_KEYS.has_key(key):
            del CACHED_KEYS[key]

        if use_default:
            return default_value
    
        raise NotCachedError(key)


def cache_set(*keys, **kwargs):
    """Set an object into the cache."""
    global CACHED_KEYS
    obj = kwargs.pop('value')
    length = kwargs.pop('length', settings.CACHE_TIMEOUT)
    skiplog = kwargs.pop('skiplog', False)

    key = cache_key(keys, **kwargs)
    val = CacheWrapper.wrap(obj)
    if not skiplog:
        log.debug('setting cache: %s', key)
    cache.set(key, val, length)
    CACHED_KEYS[key] = True



def _hash_or_string(key):
    if is_string_like(key) or isinstance(key, (types.IntType, types.LongType, types.FloatType)):
        return smart_str(key)
    else:
        try:
            #if it has a PK, use it.
            return str(key._get_pk_val())
        except AttributeError:
            return md5_hash(key)

def cache_contains(*keys, **kwargs):
    key = cache_key(keys, **kwargs)
    return CACHED_KEYS.has_key(key)

def cache_key(*keys, **pairs):
    """Smart key maker, returns the object itself if a key, else a list 
    delimited by ':', automatically hashing any non-scalar objects."""

    if is_string_like(keys):
        keys = [keys]
        
    if is_list_or_tuple(keys):
        if len(keys) == 1 and is_list_or_tuple(keys[0]):
            keys = keys[0]
    else:
        keys = [md5_hash(keys)]

    if pairs:
        keys = list(keys)
        klist = pairs.keys()
        klist.sort()
        for k in klist:
            keys.append(k)
            keys.append(pairs[k])
        
    key = KEY_DELIM.join([_hash_or_string(x) for x in keys])
    return key.replace(" ", ".")
    
def md5_hash(obj):
    pickled = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    return md5.new(pickled).hexdigest()


def is_memcached_backend():
    try:
        return cache._cache.__module__.endswith('memcache')
    except AttributeError:
        return False

def cache_require():
    """Error if caching isn't running."""
    cache_set("require_cache",value='1')
    v = cache_get('require_cache', default = '0')
    if v != '1':
        raise CacheNotRespondingError()
    else:
        log.debug("Cache responding OK")
    return True