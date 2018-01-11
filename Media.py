#! /usr/bin/python3
# coding: utf-8

import sys
import threading
import multiprocessing
import multiprocessing.connection
import random
import socket
import struct
import ast
import time
import logging
import errno
import collections
log = logging.getLogger('Media')

class Media(threading.Thread):
    defaultcodecs = {
        0 :('PCMU/8000',   None),
        3 :('GSM/8000',    None),
        4 :('G723/8000',   None),
        5 :('DVI4/8000',   None),
        6 :('DVI4/16000',  None),
        7 :('LPC/8000',    None),
        8 :('PCMA/8000',   None),
        9 :('G722/8000',   None),
        10:('L16/44100/2', None),
        11:('L16/44100/1', None),
        12:('QCELP/8000',  None),
        13:('CN/8000',     None),
        14:('MPA/90000',   None),
        15:('G728/8000',   None),
        16:('DVI4/11025',  None),
        17:('DVI4/22050',  None),
        18:('G729/8000',   None)}

    def __init__(self, ua, ip=None, port=None, pcapfilename=None, pcapfilter=None):
        self.ua = ua
        self.stopped = False
        self.localip = ip or ua.transport.localip
        self.localport = None
        self.wantedlocalport = port or 0
        self.pcapfilename = pcapfilename
        self.pcapfilter = pcapfilter
        self.codecs = [(payloadtype, codecname, codecformat) for payloadtype,(codecname, codecformat) in Media.defaultcodecs.items()]

        self.lock = multiprocessing.Lock()
        self.lock.acquire()
        self.pipe,childpipe = multiprocessing.Pipe()
        self.process = MediaProcess(pipe=childpipe, lock=self.lock)
        self.process.start()

        super().__init__(daemon=True)
        self.start()

    def getlocaloffer(self):
        if self.localport is None:
            self.opensocket(self.localip, self.wantedlocalport)
        sdplines = ['v=0',
                    'o=- {0} {0} IN IP4 0.0.0.0'.format(random.randint(0,0xffffffff)),
                    's=-',
                    'c=IN IP4 {}'.format(self.localip),
                    't=0 0',
                    'm=audio {} RTP/AVP {}'.format(self.localport, ' '.join([str(t) for t,n,f in self.codecs])),
                    'a=sendrecv'
        ]
        sdplines.extend(['a=rtpmap:{} {}'.format(t, n) for t,n,f in self.codecs if n])
        sdplines.extend(['a=fmtp:{} {}'.format(t, f) for t,n,f in self.codecs if f])
        sdplines.append('')
        return ('\r\n'.join(sdplines), 'application/sdp')

    def opensocket(self, localip, localport):
        self.pipe.send(('opensocket', (localip, localport)))
        localportorexc = self.pipe.recv()
        if isinstance(localportorexc, Exception):
            log.error("%s %s", self.process, localportorexc)
            raise localportorexc
        self.localport = localportorexc

    def setremoteoffer(self, sdp):
        remoteip = None
        remoteport = None
        for line in sdp.splitlines():
            if line.startswith(b'c='):
                remoteip = line.split()[2].decode('ascii')
            if line.startswith(b'm='):
                remoteport = int(line.split()[1])
        if remoteip is not None and remoteport is not None and self.pcapfilename is not None:
            if self.localport is None:
                self.opensocket(self.localip, self.wantedlocalport)
            self.starttransmit(remoteip, remoteport, self.pcapfilename, self.pcapfilter)
        return True

    def starttransmit(self, remoteip, remoteport, pcapfilename, pcapfilter):
        self.pipe.send(('starttransmit',((remoteip, remoteport), (pcapfilename, pcapfilter))))
        ackorexc = self.pipe.recv()
        if isinstance(ackorexc, Exception):
            log.error("%s %s", self.process, ackorexc)
            raise ackorexc

    def stop(self):
        self.stopped = True
        self.pipe.send(('stop', None))
        self.pipe.recv()

    # Thread loop
    def run(self):
        self.lock.acquire()
        if not self.stopped:
            self.ua.bye(self)

class MediaProcess(multiprocessing.Process):
    def __init__(self, pipe, lock):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.lock = lock

    def __str__(self):
        return "pid:{}".format(self.pid)

    def run(self):
        log.info("%s starting process", self)

        running = True
        transmitting = False
        sock = None
        rtpstream = None

        while running:
            # compute sleep time
            #  it depends on state:
            #   -not transmitting -> infinite = wakeup only on incomming data from pipe or socket
            #   -transmitting and wakeup time in the past -> 0 = no sleep = immediate processing
            #   -transmitting and wakeup time in the future -> wakeup time - current time
            currenttime = time.monotonic()
            if not transmitting:
                sleep = None
            else:
                if wakeuptime <= currenttime:
                    sleep = 0
                else:
                    sleep = wakeuptime - currenttime

            # wait for incomming data or timeout
            obj = None
            if sock:
                objs = [sock, self.pipe]
            else:
                objs = [self.pipe]
            for obj in multiprocessing.connection.wait(objs, sleep):
                if obj == sock:
                    # incoming data from socket
                    #  discard data (and log)
                    buf,addr = sock.recvfrom(65536)
                    rtp = RTP.frombytes(buf)
                    log.info("%s %s:%-5d <--- %s:%-5d RTP(%s)", self, *sock.getsockname(), *addr, rtp)
                        
                elif obj == self.pipe:
                    # incomming data from pipe = command from main program. possible commands:
                    #  -opensocket + localaddr:
                    #     create socket, bind it and
                    #     return its local port
                    #  -starttransmit + remoteaddr + pcap:
                    #     start transmitting
                    #     return ack
                    #  -stop:
                    #     stop transmitting and delete current socket if any
                    #     stop process
                    #     return ack
                    command,param = self.pipe.recv()
                    if command == 'opensocket':
                        localaddr = param
                        try:
                            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        except Exception as exc:
                            self.pipe.send(exc)
                        else:
                            try:
                                sock.bind(localaddr)
                                localport = sock.getsockname()[1]
                            except OSError as err:
                                sock.close()
                                sock = None
                                exc = Exception("cannot bind UDP socket to {}. errno={}".format(localaddr, errno.errorcode[err.errno]))
                                self.pipe.send(exc)
                            except Exception as exc:
                                sock.close()
                                sock = None
                                self.pipe.send(exc)
                            else:
                                self.pipe.send(localport)
                                log.info("%s start listenning on %s:%d", self, *sock.getsockname())

                    elif command == 'starttransmit':
                        remoteaddr,pcap = param
                        try:
                            rtpstream = RTPStream(*pcap)
                        except Exception as exc:
                            self.pipe.send(exc)
                        else:
                            transmitting = True
                            refrtptime = time.monotonic()
                            wakeuptime = time.monotonic()
                            self.pipe.send('started')
                            log.info("%s start transmitting to %s:%d", self, *remoteaddr)

                    elif command == 'stop':
                        transmitting = False
                        running = False

                    else:
                        self.pipe.send(Exception("Unknown command {}".format(command)))

            if obj is None:
                # multiprocessing.connection.wait timeout
                # time to send next RTP packet if there is one
                wakeuptime,rtp = rtpstream.nextpacket()
                sock.sendto(rtp, remoteaddr)
                log.info("%s %s:%-5d ---> %s:%-5d RTP(%s)", self, *sock.getsockname(), *remoteaddr, RTP.frombytes(rtp))
                if wakeuptime < 0:
                    transmitting = False
                    running = False
                    log.info("%s %s:%-5d ---| %s:%-5d EOS", self, *sock.getsockname(), *remoteaddr)
                else:
                    wakeuptime += refrtptime
        # end of while running

        self.pipe.send('stopped')
        log.info("%s stopping process", self)
        self.lock.release()
        if sock:
            sock.close()


class RTP:
    def __init__(self, payload, PT, seq, TS, SSRC, version=2, P=0, X=0, CC=0, M=0):
        self.payload = payload
        self.PT = PT
        self.seq = seq
        self.TS = TS
        self.SSRC = SSRC
        self.version,self.P,self.X,self.CC,self.M = version,P,X,CC,M

    def __str__(self):
        return "PT={} seq=0x{:x} TS=0x{:x} SSRC=0x{:x} + {}bytes".format(self.PT, self.seq, self.TS, self.SSRC, len(self.payload))

    @staticmethod
    def frombytes(buf):
        h0,h1,seq,TS,SSRC = struct.unpack_from('!bbHLL', buf[:12] + 12*b'\x00')
        version = h0>>6
        P = (h0>>5) & 0b1
        X = (h0>>4) & 0b1
        CC = h0 & 0b1111
        M = h1 >> 7
        PT = h1 & 0b01111111
        payload = buf[12:]

        return RTP(payload, PT, seq, TS, SSRC, version, P, X, CC, M)

    def tobytes(self):
        hdr = bytearray(12)
        hdr[0] = self.version<<6 | self.P<<5 | self.X<<4 | self.CC
        hdr[1] = self.M<<7 | self.PT
        struct.pack_into('!HLL', hdr, 2, self.seq, self.TS, self.SSRC)
        return hdr + self.payload


class RTPStream:
    filtercriterions = ('srcport', 'dstport', 'PT', 'SSRC')

    def __init__(self, pcapfilename, pcapfilter=None):
        self.udpstream = PcapUDPStream(pcapfilename)
        self.pcapfilter = pcapfilter or {}
        extracriterion = set(self.pcapfilter.keys()) - set(RTPStream.filtercriterions)
        if extracriterion:
            raise Exception("Unexpected filter criterion {!}".format(list(extracriterion)))
        self.eof = False
        self.generator = self._generator()
        try:
            dummy,self.nextrtp = next(self.generator)
        except StopIteration:
            self.eof = True

    def nextpacket(self):
        rtp = self.nextrtp
        try:
            wakeuptime,self.nextrtp = next(self.generator)
        except StopIteration:
            self.eof = True
            return -1,rtp
        return wakeuptime,rtp

    def _generator(self):
        inittimestamp = None
        for block in self.udpstream:
            rtp = block.data
            PT,SSRC = struct.unpack_from('!xB6xI', rtp);PT&=0x7f
            params = dict(srcport=block.srcport, dstport=block.dstport, PT=PT, SSRC=SSRC)
            for k,v in self.pcapfilter.items():
                if params[k] != v:
                    break
            else:
                if inittimestamp is None:
                    inittimestamp = block.timestamp
                    timestamp = 0
                if block.timestamp - inittimestamp < timestamp or block.timestamp - inittimestamp > timestamp + 5:
                    inittimestamp = block.timestamp - timestamp - 0.2
                timestamp = block.timestamp - inittimestamp
                yield timestamp,rtp
        self.eof=True


class PcapUDPStream:
    Datagram = collections.namedtuple('Datagram', 'timestamp srcport dstport data')
    def __init__(self, filename):
        self.error = None
        self.fp = open(filename, 'rb')
        blocktype,blockdata = self.nextblock()
        if blocktype != 0x0a0d0d0a or not self.decodeheader(blockdata):
            raise Exception("{} is not a pcapng file".format(filename))

    def __iter__(self):
        while True:
            if self.error:
                return
            blocktype,blockdata = self.nextblock()
            if blocktype == 0x0a0d0d0a:
                if not self.decodeheader(blockdata):
                    return
            elif blocktype == 1:
                self.decodeinterface(blockdata)
            elif blocktype == 6:
                block = self.decodeenhancedpacket(blockdata)
                if block:
                    yield block
            elif blocktype == None:
                return

    def nextblock(self):
        buf = self.fp.read(8)
        if len(buf) != 8:
            self.error = "truncated block"
            return None,None
        blocktype,length = struct.unpack('=LL', buf)
        if length%4 != 0:
            self.error = "bad block length"
            return None,None
        blockdata = self.fp.read(length-12)
        if len(blockdata) != length-12:
            self.error = "truncated block"
            return None,None
        buf = self.fp.read(4)
        if len(buf) != 4:
            self.error = "truncated block"
            return None,None
        length2, = struct.unpack('=L', buf)
        if length2 != length:
            self.error = "incoherent block length"
            return None,None
        return blocktype, blockdata

    def decodeheader(self, header):
        bo,major,minor = struct.unpack_from('=LHH', header)
        if minor != 0:
            self.error = "bad minor version"
        if major != 1:
            self.error = "bad major version"
        if bo != 0x1a2b3c4d:
            self.error = "bad BO magic"
        self.interfaces = []
        return self.error is None

    def decodeinterface(self, interface):
        link, = struct.unpack_from('=H', interface)
        self.interfaces.append(link)
        for optioncode,optionvalue in self.decodeoptions(interface[8:]):
            pass

    def decodeenhancedpacket(self, packet):
        interface,timestampH,timestampL,capturedlen,originallen = struct.unpack_from('=LLLLL', packet)
        packet = packet[20:20+originallen]
        if interface >= len(self.interfaces):
            self.error = "unknown interface"
            return
        if self.interfaces[interface] != 1 or capturedlen != originallen:
            return # not an Ethernet packet or truncated packet
        if len(packet) != originallen:
            self.error = "bad packet length"
            return
        ethertype, = struct.unpack_from('!h', packet, 12)
        if ethertype != 0x800:
            return # not an Ethernet/IPv4 packet
        offsetdata = 4 * (packet[14] & 0x0f)
        protocol = packet[23]
        if protocol != 17:
            return # not an Ethernet/IP/UDP
        srcport,dstport = struct.unpack_from('!2H', packet, 14 + offsetdata)
        data = packet[14 + offsetdata + 8:]
        timestamp = ((timestampH<<32) + timestampL) * 10**-6
        return PcapUDPStream.Datagram(timestamp, srcport, dstport, data)

    def decodeoptions(self, options):
        while True:
            if len(options) < 4:
                return
            code,length = struct.unpack_from(options)
            if len(options) < 4+length:
                return
            value = options[4:4+length]
            options = options[4+length:]
            yield code,value
