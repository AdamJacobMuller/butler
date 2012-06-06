""" Encoding Booth - Worker Broker Client

A 0MQ socket of type ZMQ_REQ is used by a client to send requests to and receive replies from a service.
Each request sent is round-robined among all broker services and each reply received is matched with the
last issued request.
"""

import time
import sys
import logging
import random
import json
import zmq

import ebwrkapi

REQUEST_RETRIES = 1
REQUEST_TIMEOUT = 1500

log = logging.getLogger(__name__)


class EBClient(object):

    identity = None  # identify string for socket connections
    broker = None  # string - example: tcp://127.0.0.1:5555
    context = None  # zmq.Context
    client = None  # zmq.Socket
    poller = None  # zmq.Poller
    retries = REQUEST_RETRIES  # count - number of request retries
    timeout = REQUEST_TIMEOUT  # msec - request timeout
    sequence = 0

    def __init__(self, broker):

        self.identity = "client-%04X-%04X" % ( random.randint(0, 0x10000), random.randint(0, 0x10000) )
        self.broker = broker
        self.context = zmq.Context(1)
        self.poller = zmq.Poller()
        self.connect_to_broker()

    def connect_to_broker(self):

        # close existing socket
        if self.client:
            self.poller.unregister(self.client)
            self.client.close()

        # create a ZMQ_REQ socket to send requests / receive replies
        self.client = self.context.socket(zmq.REQ)

        # set the `identity` to retrieve responses on disconnect
        self.client.setsockopt(zmq.IDENTITY, self.identity)
        self.client.setsockopt(zmq.LINGER, 0)
        self.client.setsockopt(zmq.HWM, 0)

        # register socket with poller
        self.poller.register(self.client, zmq.POLLIN)

        # connect to `Router` socket
        self.client.connect(self.broker)

    def send(self, service, request):
        """
        Returns the reply message or None if there was no reply within timeout limit
        The maximum total blocking time for this method is REQUEST_RETRIES*REQUEST_TIMEOUT in msec
        """
        time_s = time.time()

        reply = None

        self.sequence += 1

        while self.retries:

            # request timeout
            timeout = int(time.time()) + int(REQUEST_TIMEOUT / 1000)

            # convert request body dict to json structure
            if isinstance(request, dict):
                request = json.dumps(request)

            """ Request Layout:

            Frame 0: Empty (zero bytes, invisible to REQ application)
            Frame 1: "EBv1:%i" (string, representing version and request sequence number)
            Frame 2: Service name (printable string)
            Frame 3: Expiration Time (unix time in future)
            Frames 4+: Request body (opaque binary, will be converted to json string)
            """

            msg = [
                '%s:%i' % (ebwrkapi.__version__, self.sequence),
                str(service),
                str(timeout),
                str(request)
            ]

            resp = self.client.send_multipart(msg)

            try:
                socks = dict(self.poller.poll(self.timeout))
            except KeyboardInterrupt:
                self.destroy()
                break

            if socks.get(self.client) == zmq.POLLIN:

                frames = self.client.recv_multipart()

                if not frames:
                    log.warn('got empty reply back from `Broker`')
                    break

                if len(frames) > 2:

                    if frames[2].startswith(ebwrkapi.__version__):

                        # parse response
                        ident, x, service_name, function, expiration, request_body = frames

                        reply = request_body

                        # Don't try to handle errors, just assert noisily
                        # assert len(msg) >= 3
                        # compares request sequence id to be in order
                        if int(service_name.split(':')[1]) == self.sequence:
                            self.retries = REQUEST_RETRIES
                            break
                        else:
                            log.error("Malformed reply from server: %s / %s" % (self.sequence, service_name.split(':')[1]))

                            self.retries -= 1
                            reply = None

                else:
                    log.debug('got service response: %s' % frames)
                    reply = frames
                    break

            else:

                self.retries -= 1

                self.connect_to_broker()

                # wait up to REQUEST_RETRIES*buffer-timeout for acceptance of the request
                if self.retries >= 1:
                    log.debug('no response from router - re-attempting request')
                else:
                    log.debug('no response from router - aborting')

        # messure request time and ensure request takes at least REQUEST_TIMEOUT
        if self.retries != REQUEST_RETRIES:
            runtime = time.time() - time_s
            runtime = runtime * 100000  # convert to msec
            if runtime < REQUEST_TIMEOUT:
                time.sleep((REQUEST_TIMEOUT - runtime) / 1000)

        # reset attempt counter
        if not self.retries:
            self.retries = REQUEST_RETRIES

        return reply

    def destroy(self):
        self.context.term()

