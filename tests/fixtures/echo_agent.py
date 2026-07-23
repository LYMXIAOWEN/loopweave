from __future__ import print_function

import os
import sys


print("READY pid={}".format(os.getpid()), flush=True)
for line in sys.stdin:
    text = line.rstrip("\r\n")
    print("RECEIVED pid={} text={}".format(os.getpid(), text), flush=True)
    if text == "EXIT":
        break
