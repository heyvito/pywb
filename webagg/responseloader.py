from webagg.liverec import BaseRecorder
from webagg.liverec import request as remote_request

from webagg.utils import MementoUtils

from pywb.utils.timeutils import timestamp_to_datetime, datetime_to_http_date
from pywb.utils.timeutils import iso_date_to_datetime
from pywb.utils.wbexception import LiveResourceException
from pywb.warc.resolvingloader import ResolvingLoader

from io import BytesIO

import uuid
import six


#=============================================================================
class StreamIter(six.Iterator):
    def __init__(self, stream, header=None, size=8192):
        self.stream = stream
        self.header = header
        self.size = size

    def __iter__(self):
        return self

    def __next__(self):
        if self.header:
            header = self.header
            self.header = None
            return header

        data = self.stream.read(self.size)
        if data:
            return data

        self.close()
        raise StopIteration

    def close(self):
        if not self.stream:
            return

        try:
            self.stream.close()
            self.stream = None
        except Exception:
            pass


#=============================================================================
class BaseLoader(object):
    def __call__(self, cdx, params):
        out_headers, res = self._load_resource(cdx, params)
        if not res:
            return None, None

        out_headers['WARC-Coll'] = cdx.get('source', '')

        out_headers['Link'] = MementoUtils.make_link(
                                     out_headers['WARC-Target-URI'],
                                     'original')

        memento_dt = iso_date_to_datetime(out_headers['WARC-Date'])
        out_headers['Memento-Datetime'] = datetime_to_http_date(memento_dt)
        return out_headers, res

    def _load_resource(self, cdx, params):  #pragma: no cover
        raise NotImplemented()


#=============================================================================
class WARCPathLoader(BaseLoader):
    def __init__(self, paths, cdx_source):
        self.paths = paths
        if isinstance(paths, str):
            self.paths = [paths]

        self.path_checks = list(self.warc_paths())

        self.resolve_loader = ResolvingLoader(self.path_checks,
                                              no_record_parse=True)
        self.cdx_source = cdx_source

    def cdx_index_source(self, *args, **kwargs):
        cdx_iter, errs = self.cdx_source(*args, **kwargs)
        return cdx_iter

    def warc_paths(self):
        for path in self.paths:
            def check(filename, cdx):
                try:
                    if hasattr(cdx, '_formatter') and cdx._formatter:
                        full_path = cdx._formatter.format(path)
                    else:
                        full_path = path
                    full_path += filename
                    return full_path
                except KeyError:
                    return None

            yield check

    def _load_resource(self, cdx, params):
        if not cdx.get('filename') or cdx.get('offset') is None:
            return None, None

        cdx._formatter = params.get('_formatter')
        failed_files = []
        headers, payload = (self.resolve_loader.
                             load_headers_and_payload(cdx,
                                                      failed_files,
                                                      self.cdx_index_source))

        record = payload
        out_headers = {}

        for n, v in record.rec_headers.headers:
            out_headers[n] = v

        if headers != payload:
            out_headers['WARC-Target-URI'] = headers.rec_headers.get_header('WARC-Target-URI')
            out_headers['WARC-Date'] = headers.rec_headers.get_header('WARC-Date')
            out_headers['WARC-Refers-To-Target-URI'] = payload.rec_headers.get_header('WARC-Target-URI')
            out_headers['WARC-Refers-To-Date'] = payload.rec_headers.get_header('WARC-Date')
            headers.stream.close()

        return out_headers, StreamIter(record.stream)

    def __str__(self):
        return  'WARCPathLoader'


#=============================================================================
class HeaderRecorder(BaseRecorder):
    def __init__(self, skip_list=None):
        self.buff = BytesIO()
        self.skip_list = skip_list
        self.skipped = []
        self.target_ip = None

    def write_response_header_line(self, line):
        if self.accept_header(line):
            self.buff.write(line)

    def get_header(self):
        return self.buff.getvalue()

    def accept_header(self, line):
        if self.skip_list and line.lower().startswith(self.skip_list):
            self.skipped.append(line)
            return False

        return True

    def finish_request(self, socket):
        ip = socket.getpeername()
        if ip:
            self.target_ip = ip[0]


#=============================================================================
class LiveWebLoader(BaseLoader):
    SKIP_HEADERS = (b'link',
                    b'memento-datetime',
                    b'content-location',
                    b'x-archive')

    def _load_resource(self, cdx, params):
        load_url = cdx.get('load_url')
        if not load_url:
            return None, None

        recorder = HeaderRecorder(self.SKIP_HEADERS)

        input_req = params['_input_req']

        req_headers = input_req.get_req_headers()

        dt = timestamp_to_datetime(cdx['timestamp'])

        if not cdx.get('is_live'):
            req_headers['Accept-Datetime'] = datetime_to_http_date(dt)

        # if different url, ensure origin is not set
        # may need to add other headers
        if load_url != cdx['url']:
            if 'Origin' in req_headers:
                splits = urlsplit(load_url)
                req_headers['Origin'] = splits.scheme + '://' + splits.netloc

        method = input_req.get_req_method()
        data = input_req.get_req_body()

        try:
            upstream_res = remote_request(url=load_url,
                                          method=method,
                                          recorder=recorder,
                                          stream=True,
                                          allow_redirects=False,
                                          headers=req_headers,
                                          data=data,
                                          timeout=params.get('_timeout'))
        except Exception:
            raise LiveResourceException(load_url)

        resp_headers = recorder.get_header()

        out_headers = {}
        out_headers['Content-Type'] = 'application/http; msgtype=response'

        out_headers['WARC-Type'] = 'response'
        out_headers['WARC-Record-ID'] = self._make_warc_id()
        out_headers['WARC-Target-URI'] = cdx['url']
        out_headers['WARC-Date'] = self._make_date(dt)
        if recorder.target_ip:
            out_headers['WARC-IP-Address'] = recorder.target_ip

        # Try to set content-length, if it is available and valid
        try:
            content_len = int(upstream_res.headers.get('content-length', 0))
            if content_len > 0:
                content_len += len(resp_headers)
                out_headers['Content-Length'] = content_len
        except (KeyError, TypeError):
            pass

        return out_headers, StreamIter(upstream_res.raw, header=resp_headers)

    @staticmethod
    def _make_date(dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def _make_warc_id(id_=None):
        if not id_:
            id_ = uuid.uuid1()
        return '<urn:uuid:{0}>'.format(id_)

    def __str__(self):
        return  'LiveWebLoader'

