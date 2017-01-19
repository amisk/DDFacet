import os, os.path, cPickle, re

import NpShared
import numpy as np


SHM_PREFIX = "/dev/shm/"
SHM_PREFIX_LEN = len(SHM_PREFIX)

def _to_shm (path):
    """Helper function, converts /dev/shm/name to shm://name"""
##    return "shm://" + path[SHM_PREFIX_LEN:]
    # it seems that shm_open() does not support subdirectories. open("/dev/shm/a") should have
    # the same effect though (even if it is Linux-specific), so use that instead
    return "file://" + path

_allowed_key_types = dict(int=int, str=str, bool=bool)

class SharedDict (dict):
    basepath = SHM_PREFIX

    @staticmethod
    def setBaseName(name):
        SharedDict.basepath = os.path.join(SHM_PREFIX, name)
        if not os.path.exists(SharedDict.basepath):
            os.mkdir(SharedDict.basepath)

    def __init__ (self, path, reset=True):
        dict.__init__(self)
        if path.startswith(SharedDict.basepath):
            self.path = path
        else:
            self.path = os.path.join(SharedDict.basepath, path)
        if reset or not os.path.exists(self.path):
            self.clear()
        else:
            self.reload()

    def delete(self):
        self.clear()
        if os.path.exists(self.path):
            os.system("rm -fr " + self.path)

    def clear(self):
        dict.clear(self)
        if os.path.exists(self.path):
            os.system("rm -fr " + self.path)
        os.mkdir(self.path)

    def reload(self):
        """(Re)initializes dict with items from path"""
        dict.clear(self)
        # scan our subdirectory for items
        for name in os.listdir(self.path):
            filepath = os.path.join(self.path, name)
            match = re.match("^(\w+):(.*):(p|a|d)$", name)
            if not match:
                print "Can't parse shared dict entry " + filepath
            keytype, key, valuetype = match.groups()
            typefunc = _allowed_key_types.get(keytype)
            if typefunc is None:
                print "Unknown shared dict key type "+keytype
            key = typefunc(key)
            # 'd' item -- is a nested SharedDict
            if valuetype == 'd':
                dict.__setitem__(self, key, SharedDict(path=filepath, reset=False))
            # pickle item -- load directly
            elif valuetype == 'p':
                dict.__setitem__(self, key, cPickle.load(file(filepath)))
            # array item -- attach as shared
            elif valuetype == 'a':
                # strip off /dev/shm/ at beginning of path
                dict.__setitem__(self, key, NpShared.GiveArray(_to_shm(filepath)))

    def _key_to_name (self, item):
        return "%s:%s:" % (type(item).__name__, str(item))

    def __setitem__ (self, item, value):
        if type(item).__name__ not in _allowed_key_types:
            raise KeyError,"unsupported key of type "+type(item).__name__
        dict.__setitem__ (self, item, value)
        name = self._key_to_name(item)
        # for arrays, copy to a shared array
        if isinstance(value, np.ndarray):
            NpShared.ToShared(_to_shm(os.path.join(self.path, name)+'a'), value)
        # for shared dicts, force use of addSubDict
        elif isinstance(value, SharedDict):
            raise TypeError,"shared sub-dicts must be initialized with addSubDict"
        # all other types, just use pickle
        else:
            cPickle.dump(value, file(os.path.join(self.path, name+'p'), "w"), 2)

    def addSubDict (self, item):
        name = self._key_to_name(item) + 'd'
        filepath = os.path.join(self.path, name)
        subdict = SharedDict(filepath, reset=True)
        dict.__setitem__(self, item, subdict)
        return subdict

    def addSharedArray (self, item, shape, dtype):
        """adds a SharedArray entry of the specified shape and dtype"""
        name = self._key_to_name(item) + 'a'
        filepath = os.path.join(self.path, name)
        array = NpShared.CreateShared(_to_shm(filepath), shape, dtype)
        dict.__setitem__(self, item, array)
        return array


def testSharedDict ():
    dic = SharedDict("foo")
    dic['a'] = 'a'
    dic['b'] = (1,2,3)
    dic['c'] = np.array([1,2,3,4])
    subdict = dic.addSubDict('subdict')
    subdict['a'] = 'aa'
    subdict['b'] = ('a', 1, 2, 3)
    subdict['c'] = np.array([1, 2, 3, 4, 5, 6])
    subdict2 = subdict.addSubDict('subdict2')
    subdict2['a'] = 'aaa'

    arr = subdict.addSharedArray("foo",(4, 4), np.float32)
    arr.fill(1)

    print dic

    other_view = SharedDict("foo", reset=False)
    print other_view
