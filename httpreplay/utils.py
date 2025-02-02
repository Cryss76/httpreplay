# Copyright (C) 2018 Jurriaan Bremer <jbr@cuckoo.sh>
# This file is part of HTTPReplay - http://jbremer.org/httpreplay/
# See the file 'LICENSE' for copying permission.

import hashlib
import logging
import io
import uuid

from httpreplay.protohandlers import http_handler, https_handler
from httpreplay.misc import read_tlsmaster
from httpreplay.reader import PcapReader
from httpreplay.transport import TCPPacketStreamer

log = logging.getLogger(__name__)

def pcap2mitm(pcapfile, mitmfile, tlsmaster=None, stream=False):
    try:
        from mitmproxy import io as mitm_io, http, connections, exceptions
        from mitmproxy.net.http import http1
    except ImportError as e:
        log.warning(
            "In order to use this utility it is required to have the "
            "mitmproxy tool installed (`pip install httpreplay[mitmproxy]`) : Error is '%s'", e
        )
        return False

    if tlsmaster:
        tlsmaster = read_tlsmaster(tlsmaster)
    else:
        tlsmaster = {}

    handlers = {
        443: lambda: https_handler(tlsmaster),
        4443: lambda: https_handler(tlsmaster),
        "generic": http_handler,
    }

    reader = PcapReader(pcapfile)
    reader.tcp = TCPPacketStreamer(reader, handlers)
    writer = mitm_io.FlowWriter(mitmfile)

    l = reader.process()
    if not stream:
        # Sort the http/https requests and responses by their timestamp.
        l = sorted(l, key=lambda x: x[1])

    for s, ts, protocol, sent, recv in l:
        if protocol not in ("http", "https"):
            continue

        srcip, srcport, dstip, dstport = s

        client_conn = connections.ClientConnection.make_dummy((srcip, srcport))
        client_conn.timestamp_start = ts

        server_conn = connections.ServerConnection.make_dummy((dstip, dstport))
        server_conn.timestamp_start = ts

        flow = http.HTTPFlow(client_conn, server_conn)

        try:
            sent = io.BytesIO(sent.raw)
            request = http1.read_request_head(sent)
            body_size = http1.expected_http_body_size(request)
            request.data.content = b"".join(http1.read_body(sent, body_size, None))
        except exceptions.HttpException as e:
            log.warning("Error parsing HTTP request: %s", e)
            continue

        flow.request = http.HTTPRequest.wrap(request)
        flow.request.timestamp_start = client_conn.timestamp_start

        flow.request.host = dstip
        flow.request.port = dstport
        flow.request.scheme = protocol

        try:
            recv = io.BytesIO(recv.raw)
            response = http1.read_response_head(recv)
            body_size = http1.expected_http_body_size(request, response)
            response.data.content = b"".join(http1.read_body(recv, body_size, None))
        except exceptions.HttpException as e:
            log.warning("Error parsing HTTP response: %s", e)
            # Fall through (?)

        flow.response = http.HTTPResponse.wrap(response)
        flow.response.timestamp_start = server_conn.timestamp_start

        flow.id = uuid.UUID(bytes=hashlib.md5(b"%d%d%s%s" % (
            client_conn.timestamp_start, server_conn.timestamp_start,
            request.data.content, response.data.content
        )).digest())

        writer.add(flow)
    return True
