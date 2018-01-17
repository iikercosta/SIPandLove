#! /usr/bin/python3
# coding: utf-8

import random
import logging
log = logging.getLogger('Dialog')

from . import Message

def UACid(message):
    totag = message.totag
    if not totag:
        return None
    return "{}/{}/{}".format(message.callid, message.fromtag, totag)

def UASid(message):
    totag = message.totag
    if not totag:
        return None
    return "{}/{}/{}".format(message.callid, totag, message.fromtag)


class Dialog:
    def __init__(self, request, response, uac=False, uas=False):
        if (uac and uas) or (not uac and not uas):
            raise ValueError("uac xor uas must be True")
        self.callid = request.callid
        if uac:
            self.remotetarget = response.getheader('contact').address
            self.localuri     = request.getheader('from').address
            self.localtag     = request.fromtag
            self.localseq     = request.seq
            self.remoteuri    = response.getheader('to').address
            self.remotetag    = response.totag
            self.remoteseq    = None
        if uas:
            self.remotetarget = request.getheader('contact').address
            self.localuri     = response.getheader('to').address
            self.localtag     = response.totag
            self.localseq     = random.randint(0,0x7fff)
            self.remoteuri    = request.getheader('from').address
            self.remotetag    = request.fromtag
            self.remoteseq    = request.seq
    @property
    def ident(self):
        return "{}/{}/{}".format(self.callid, self.localtag, self.remotetag)
