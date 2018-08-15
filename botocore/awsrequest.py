# Copyright (c) 2012-2013 Mitch Garnaat http://garnaat.org/
# Copyright 2012-2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import sys
import logging
import functools
import socket
import collections

import urllib3.util
from urllib3.connection import VerifiedHTTPSConnection
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.connectionpool import HTTPSConnectionPool

import botocore.utils
from botocore.compat import six
from botocore.compat import HTTPHeaders, HTTPResponse, urlunsplit, urlsplit, \
     urlencode
from botocore.exceptions import UnseekableStreamError


logger = logging.getLogger(__name__)


class AWSHTTPResponse(HTTPResponse):
    # The *args, **kwargs is used because the args are slightly
    # different in py2.6 than in py2.7/py3.
    def __init__(self, *args, **kwargs):
        self._status_tuple = kwargs.pop('status_tuple')
        HTTPResponse.__init__(self, *args, **kwargs)

    def _read_status(self):
        if self._status_tuple is not None:
            status_tuple = self._status_tuple
            self._status_tuple = None
            return status_tuple
        else:
            return HTTPResponse._read_status(self)


class AWSConnection(object):
    """Mixin for HTTPConnection that supports Expect 100-continue.

    This when mixed with a subclass of httplib.HTTPConnection (though
    technically we subclass from urllib3, which subclasses
    httplib.HTTPConnection) and we only override this class to support Expect
    100-continue, which we need for S3.  As far as I can tell, this is
    general purpose enough to not be specific to S3, but I'm being
    tentative and keeping it in botocore because I've only tested
    this against AWS services.

    """
    def __init__(self, *args, **kwargs):
        super(AWSConnection, self).__init__(*args, **kwargs)
        self._original_response_cls = self.response_class
        # We'd ideally hook into httplib's states, but they're all
        # __mangled_vars so we use our own state var.  This variable is set
        # when we receive an early response from the server.  If this value is
        # set to True, any calls to send() are noops.  This value is reset to
        # false every time _send_request is called.  This is to workaround the
        # fact that py2.6 (and only py2.6) has a separate send() call for the
        # body in _send_request, as opposed to endheaders(), which is where the
        # body is sent in all versions > 2.6.
        self._response_received = False
        self._expect_header_set = False

    def close(self):
        super(AWSConnection, self).close()
        # Reset all of our instance state we were tracking.
        self._response_received = False
        self._expect_header_set = False
        self.response_class = self._original_response_cls

    def _tunnel(self):
        # Works around a bug in py26 which is fixed in later versions of
        # python. Bug involves hitting an infinite loop if readline() returns
        # nothing as opposed to just ``\r\n``.
        # As much as I don't like having if py2: <foo> code blocks, this seems
        # the cleanest way to handle this workaround.  Fortunately, the
        # difference from py26 to py3 is very minimal.  We're essentially
        # just overriding the while loop.
        if sys.version_info[:2] != (2, 6):
            return super(AWSConnection, self)._tunnel()

        # Otherwise we workaround the issue.
        self._set_hostport(self._tunnel_host, self._tunnel_port)
        self.send("CONNECT %s:%d HTTP/1.0\r\n" % (self.host, self.port))
        for header, value in self._tunnel_headers.iteritems():
            self.send("%s: %s\r\n" % (header, value))
        self.send("\r\n")
        response = self.response_class(self.sock, strict=self.strict,
                                       method=self._method)
        (version, code, message) = response._read_status()

        if code != 200:
            self.close()
            raise socket.error("Tunnel connection failed: %d %s" %
                               (code, message.strip()))
        while True:
            line = response.fp.readline()
            if not line:
                break
            if line in (b'\r\n', b'\n', b''):
                break

    def _send_request(self, method, url, body, headers, *args, **kwargs):
        self._response_received = False
        if headers.get('Expect', b'') == b'100-continue':
            self._expect_header_set = True
        else:
            self._expect_header_set = False
            self.response_class = self._original_response_cls
        rval = super(AWSConnection, self)._send_request(
            method, url, body, headers, *args, **kwargs)
        self._expect_header_set = False
        return rval

    def _convert_to_bytes(self, mixed_buffer):
        # Take a list of mixed str/bytes and convert it
        # all into a single bytestring.
        # Any six.text_types will be encoded as utf-8.
        bytes_buffer = []
        for chunk in mixed_buffer:
            if isinstance(chunk, six.text_type):
                bytes_buffer.append(chunk.encode('utf-8'))
            else:
                bytes_buffer.append(chunk)
        msg = b"\r\n".join(bytes_buffer)
        return msg

    def _send_output(self, message_body=None, *args, **kwargs):
        self._buffer.extend((b"", b""))
        msg = self._convert_to_bytes(self._buffer)
        del self._buffer[:]
        # If msg and message_body are sent in a single send() call,
        # it will avoid performance problems caused by the interaction
        # between delayed ack and the Nagle algorithm.
        if isinstance(message_body, bytes):
            msg += message_body
            message_body = None
        self.send(msg)
        if self._expect_header_set:
            # This is our custom behavior.  If the Expect header was
            # set, it will trigger this custom behavior.
            logger.debug("Waiting for 100 Continue response.")
            # Wait for 1 second for the server to send a response.
            if urllib3.util.wait_for_read(self.sock, 1):
                self._handle_expect_response(message_body)
                return
            else:
                # From the RFC:
                # Because of the presence of older implementations, the
                # protocol allows ambiguous situations in which a client may
                # send "Expect: 100-continue" without receiving either a 417
                # (Expectation Failed) status or a 100 (Continue) status.
                # Therefore, when a client sends this header field to an origin
                # server (possibly via a proxy) from which it has never seen a
                # 100 (Continue) status, the client SHOULD NOT wait for an
                # indefinite period before sending the request body.
                logger.debug("No response seen from server, continuing to "
                             "send the response body.")
        if message_body is not None:
            # message_body was not a string (i.e. it is a file), and
            # we must run the risk of Nagle.
            self.send(message_body)

    def _consume_headers(self, fp):
        # Most servers (including S3) will just return
        # the CLRF after the 100 continue response.  However,
        # some servers (I've specifically seen this for squid when
        # used as a straight HTTP proxy) will also inject a
        # Connection: keep-alive header.  To account for this
        # we'll read until we read '\r\n', and ignore any headers
        # that come immediately after the 100 continue response.
        current = None
        while current != b'\r\n':
            current = fp.readline()

    def _handle_expect_response(self, message_body):
        # This is called when we sent the request headers containing
        # an Expect: 100-continue header and received a response.
        # We now need to figure out what to do.
        fp = self.sock.makefile('rb', 0)
        try:
            maybe_status_line = fp.readline()
            parts = maybe_status_line.split(None, 2)
            if self._is_100_continue_status(maybe_status_line):
                self._consume_headers(fp)
                logger.debug("100 Continue response seen, "
                             "now sending request body.")
                self._send_message_body(message_body)
            elif len(parts) == 3 and parts[0].startswith(b'HTTP/'):
                # From the RFC:
                # Requirements for HTTP/1.1 origin servers:
                #
                # - Upon receiving a request which includes an Expect
                #   request-header field with the "100-continue"
                #   expectation, an origin server MUST either respond with
                #   100 (Continue) status and continue to read from the
                #   input stream, or respond with a final status code.
                #
                # So if we don't get a 100 Continue response, then
                # whatever the server has sent back is the final response
                # and don't send the message_body.
                logger.debug("Received a non 100 Continue response "
                             "from the server, NOT sending request body.")
                status_tuple = (parts[0].decode('ascii'),
                                int(parts[1]), parts[2].decode('ascii'))
                response_class = functools.partial(
                    AWSHTTPResponse, status_tuple=status_tuple)
                self.response_class = response_class
                self._response_received = True
        finally:
            fp.close()

    def _send_message_body(self, message_body):
        if message_body is not None:
            self.send(message_body)

    def send(self, str):
        if self._response_received:
            logger.debug("send() called, but reseponse already received. "
                         "Not sending data.")
            return
        return super(AWSConnection, self).send(str)

    def _is_100_continue_status(self, maybe_status_line):
        parts = maybe_status_line.split(None, 2)
        # Check for HTTP/<version> 100 Continue\r\n
        return (
            len(parts) >= 3 and parts[0].startswith(b'HTTP/') and
            parts[1] == b'100')


class AWSHTTPConnection(AWSConnection, HTTPConnection):
    """ An HTTPConnection that supports 100 Continue behavior. """


class AWSHTTPSConnection(AWSConnection, VerifiedHTTPSConnection):
    """ An HTTPSConnection that supports 100 Continue behavior. """


def prepare_request_dict(request_dict, endpoint_url, context=None,
                         user_agent=None):
    """
    This method prepares a request dict to be created into an
    AWSRequestObject. This prepares the request dict by adding the
    url and the user agent to the request dict.

    :type request_dict: dict
    :param request_dict:  The request dict (created from the
        ``serialize`` module).

    :type user_agent: string
    :param user_agent: The user agent to use for this request.

    :type endpoint_url: string
    :param endpoint_url: The full endpoint url, which contains at least
        the scheme, the hostname, and optionally any path components.
    """
    r = request_dict
    if user_agent is not None:
        headers = r['headers']
        headers['User-Agent'] = user_agent
    url = _urljoin(endpoint_url, r['url_path'])
    if r['query_string']:
        # NOTE: This is to avoid circular import with utils. This is being
        # done to avoid moving classes to different modules as to not cause
        # breaking chainges.
        percent_encode_sequence = botocore.utils.percent_encode_sequence
        encoded_query_string = percent_encode_sequence(r['query_string'])
        if '?' not in url:
            url += '?%s' % encoded_query_string
        else:
            url += '&%s' % encoded_query_string
    r['url'] = url
    r['context'] = context
    if context is None:
        r['context'] = {}


def create_request_object(request_dict):
    """
    This method takes a request dict and creates an AWSRequest object
    from it.

    :type request_dict: dict
    :param request_dict:  The request dict (created from the
        ``prepare_request_dict`` method).

    :rtype: ``botocore.awsrequest.AWSRequest``
    :return: An AWSRequest object based on the request_dict.

    """
    r = request_dict
    request_object = AWSRequest(
        method=r['method'], url=r['url'], data=r['body'], headers=r['headers'])
    request_object.context.update(r['context'])
    return request_object


def _urljoin(endpoint_url, url_path):
    p = urlsplit(endpoint_url)
    # <part>   - <index>
    # scheme   - p[0]
    # netloc   - p[1]
    # path     - p[2]
    # query    - p[3]
    # fragment - p[4]
    if not url_path or url_path == '/':
        # If there's no path component, ensure the URL ends with
        # a '/' for backwards compatibility.
        if not p[2]:
            return endpoint_url + '/'
        return endpoint_url
    if p[2].endswith('/') and url_path.startswith('/'):
        new_path = p[2][:-1] + url_path
    else:
        new_path = p[2] + url_path
    reconstructed = urlunsplit((p[0], p[1], new_path, p[3], p[4]))
    return reconstructed


class AWSRequest(object):
    """Represents the elements of an HTTP request.

    This class is originally inspired by requests.models.Request, but has been
    boiled down to meet the specific use cases in botocore. That being said this
    class (even in requests) is effectively a named-tuple.
    """

    def __init__(self,
                 method=None,
                 url=None,
                 headers=None,
                 data=None,
                 params=None,
                 auth_path=None,
                 stream_output=False):

        # Default empty dicts for dict params.
        params = {} if params is None else params

        self.method = method
        self.url = url
        self.headers = HTTPHeaders()
        self.data = data
        self.params = params
        self.auth_path = auth_path
        self.stream_output = stream_output

        if headers is not None:
            for key, value in headers.items():
                self.headers[key] = value

        # This is a dictionary to hold information that is used when
        # processing the request. What is inside of ``context`` is open-ended.
        # For example, it may have a timestamp key that is used for holding
        # what the timestamp is when signing the request. Note that none
        # of the information that is inside of ``context`` is directly
        # sent over the wire; the information is only used to assist in
        # creating what is sent over the wire.
        self.context = {}

    def prepare(self):
        """Constructs a :class:`AWSPreparedRequest <AWSPreparedRequest>`."""
        return AWSPreparedRequest(self)

    @property
    def body(self):
        p = AWSPreparedRequest(self)
        p.prepare_body(self.data)
        if isinstance(p.body, six.text_type):
            p.body = p.body.encode('utf-8')
        return p.body


class AWSPreparedRequest(object):
    """Represents a prepared request.

    This class is originally inspired by requests.models.PreparedRequest, but
    has been boiled down to meet the specific use cases in botocore. Of note
    there are the following differences:
        This class does not heavily prepare the URL. Requests performed many
        validations and corrections to ensure the URL is properly formatted.
        Botocore either performs these validations elsewhere or otherwise
        consistently provides well formatted URLs.

        This class does not heavily prepare the body. Body preperation is
        simple and supports only the cases that we document: bytes and
        file-like objects to determine the content-length. This will also
        additionally prepare a body that is a dict to be url encoded params
        string as some signers rely on this. Finally, this class does not
        support multipart file uploads.

        This class does not prepare the method, auth or cookies.

    :ivar method: HTTP Method
    :ivar url: The full url
    :ivar headers: The HTTP headers to send.
    :ivar body: The HTTP body.
    :ivar stream_output: If the response for this request should be streamed.
    :ivar original: The original AWSRequest
    """
    def __init__(self, original):
        self.method = original.method
        self.prepare_url(original.url, original.params)
        self.prepare_headers(original.headers)
        self.prepare_body(original.data)
        self.stream_output = original.stream_output
        self.original = original

    def __repr__(self):
        fmt = (
            '<AWSPreparedRequest stream_output=%s, method=%s, url=%s, '
            'headers=%s>'
        )
        return fmt % (self.stream_output, self.method, self.url, self.headers)

    def reset_stream(self):
        # Trying to reset a stream when there is a no stream will
        # just immediately return.  It's not an error, it will produce
        # the same result as if we had actually reset the stream (we'll send
        # the entire body contents again if we need to).
        # Same case if the body is a string/bytes type.
        if self.body is None or isinstance(self.body, six.text_type) or \
           isinstance(self.body, six.binary_type):
            return
        try:
            logger.debug("Rewinding stream: %s", self.body)
            self.body.seek(0)
        except Exception as e:
            logger.debug("Unable to rewind stream: %s", e)
            raise UnseekableStreamError(stream_object=self.body)

    def prepare_url(self, url, params):
        if params:
            params = urlencode(list(params.items()), doseq=True)
            self.url = '%s?%s' % (url, params)
        else:
            self.url = url

    def prepare_headers(self, headers):
        headers = headers or {}
        self.headers = HeadersDict(headers.items())

    def prepare_body(self, data):
        """Prepares the given HTTP body data."""
        self.body = data

        if self.body == b'':
            self.body = None

        if not self.body and self.method == 'GET':
            return

        if isinstance(self.body, dict):
            params = list(self.body.items())
            self.body = urlencode(params, doseq=True)

        try:
            length = len(self.body)
            self.headers['Content-Length'] = str(length)
        except (AttributeError, TypeError) as e:
            pass

        if 'Content-Length' not in self.headers:
            if hasattr(data, 'seek') and hasattr(data, 'tell'):
                orig_pos = data.tell()
                data.seek(0, 2)
                end_file_pos = data.tell()
                self.headers['Content-Length'] = str(end_file_pos - orig_pos)
                data.seek(orig_pos)

        if self.body and 'Content-Length' not in self.headers:
            # NOTE: This should probably never happen, we don't use chunked
            self.headers['Transfer-Encoding'] = 'chunked'


class AWSResponse(object):
    """
    This class is originally inspired by requests.models.Response, but
    has been boiled down to meet the specific use cases in botocore. This
    has effectively been reduced to a named tuple for our use case. Most of
    the more interesting functionality from the requests version has been
    put onto our botocore.response.StreamingBody class.
    """

    def __init__(self, url, status_code, headers, raw):
        self.url = url
        self.status_code = status_code
        self.headers = HeadersDict(headers)
        self.raw = raw

        self._content = None

    @property
    def content(self):
        """Content of the response, in bytes."""

        if self._content is None:
            # Read the contents.
            # NOTE: requests would attempt to call stream and fall back
            # to a custom generator that would call read in a loop, but
            # we don't rely on this behavior
            self._content = bytes().join(self.raw.stream()) or bytes()

        return self._content

    @property
    def text(self):
        encoding = botocore.utils.get_encoding_from_headers(self.headers)
        if encoding:
            return self.content.decode(encoding)
        else:
            return self.content.decode()


class _HeaderKey(object):
    def __init__(self, key):
        self._key = key
        self._lower = key.lower()

    def __hash__(self):
        return hash(self._lower)

    def __eq__(self, other):
        return isinstance(other, _HeaderKey) and self._lower == other._lower

    def __str__(self):
        return self._key

    def __repr__(self):
        return repr(self._key)


class HeadersDict(collections.MutableMapping):
    """A case-insenseitive dictionary to represent HTTP headers. """
    def __init__(self, *args, **kwargs):
        self._dict = {}
        self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        self._dict[_HeaderKey(key)] = value

    def __getitem__(self, key):
        return self._dict[_HeaderKey(key)]

    def __delitem__(self, key):
        del self._dict[_HeaderKey(key)]

    def __iter__(self):
        return (str(key) for key in self._dict)

    def __len__(self):
        return len(self._dict)

    def __repr__(self):
        return repr(self._dict)

    def copy(self):
        return HeadersDict(self.items())


HTTPSConnectionPool.ConnectionCls = AWSHTTPSConnection
HTTPConnectionPool.ConnectionCls = AWSHTTPConnection
